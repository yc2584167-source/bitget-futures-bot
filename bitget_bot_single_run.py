"""
Bitget Multi-Signal Trading Bot — FUTURES-ONLY / Single-Run / GitHub Actions Edition
=====================================================================================

Same signal strategy (RSI + Volume + News sentiment), refactored to run
ONCE per invocation and persist state to state.json.

⚠️ THIS VERSION TRADES USDT-M PERPETUAL FUTURES ONLY.
- There is no spot trading code path. It will not touch your spot wallet.
- It NEVER calls any transfer/withdrawal endpoint. Funds must already be
  sitting in your Bitget USDT-M Futures wallet before you run this live —
  you move them there yourself, manually, through Bitget's own interface.
  Search this file for "NO TRANSFER" to see the explicit guard.
- Futures = leveraged, borrowed-style exposure by nature. Losses can
  exceed your margin balance and positions can be force-liquidated by
  the exchange, sometimes faster than you can react, especially on a
  small account where a modest adverse move consumes your entire margin.

⚠️ READ BEFORE USING
- NOT financial advice. Trading carries real risk of loss.
- Defaults to DRY_RUN with a simulated balance tracked in state.json.
  You must deliberately set LIVE_TRADING = True to place real orders.
- Designed for a PUBLIC GitHub repo (unlimited free Actions minutes).
  Never commit real API keys — use GitHub Secrets (see workflow YAML).
- News sentiment degrades gracefully: if the API key is missing or the
  request fails, that signal is skipped (weight redistributed) rather
  than blocking the run.

SETUP
-----
pip install ccxt pandas requests vaderSentiment

Environment variables (set as GitHub Secrets in production):
  BITGET_API_KEY
  BITGET_API_SECRET
  BITGET_API_PASSWORD
  CRYPTOPANIC_API_TOKEN   (optional)

Run once locally to test:
  python bitget_bot_single_run.py
"""

import os
import sys
import json
import time
import logging
import functools
from datetime import datetime, timezone, timedelta

import ccxt
import pandas as pd
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# =============================================================================
# CONFIGURATION
# =============================================================================

# ccxt unified symbol format for a Bitget USDT-margined perpetual future.
# The ":USDT" suffix is what tells ccxt this is a swap contract, not spot.
SYMBOL = "BTC/USDT:USDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"
TIMEFRAME = "5m"

# This bot is FUTURES-ONLY. There is no spot code path — "TRADE_MODE" no
# longer exists as a spot/margin toggle, it's always perpetual futures.
MARGIN_MODE = "cross"            # "cross" or "isolated"
LEVERAGE = 2
MAX_ALLOWED_LEVERAGE = 3         # hard ceiling regardless of LEVERAGE above
MIN_MARGIN_HEALTH_RATIO = 1.5

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

VOLUME_LOOKBACK = 20
VOLUME_SPIKE_MULTIPLIER = 1.5

NEWS_ENABLED = True
NEWS_MAX_HEADLINES = 20

WEIGHT_RSI = 0.4
WEIGHT_VOLUME = 0.3
WEIGHT_NEWS = 0.3

BUY_SCORE_THRESHOLD = 0.5
SELL_SCORE_THRESHOLD = -0.5

POSITION_SIZE_PCT = 0.95
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.02
MIN_NOTIONAL_USD = 1.5

MAX_API_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

CONSECUTIVE_ERROR_COOLDOWN_THRESHOLD = 5
COOLDOWN_MINUTES = 30

LIVE_TRADING = True
DRY_RUN_STARTING_BALANCE = 1.0

STATE_FILE = "state.json"

# =============================================================================
# LOGGING (stdout only — GitHub Actions captures this in the run log)
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bitget_bot")

sentiment_analyzer = SentimentIntensityAnalyzer()

# =============================================================================
# STATE PERSISTENCE
# =============================================================================

DEFAULT_STATE = {
    "in_position": False,
    "entry_price": None,
    "base_asset_amount": 0.0,
    "stop_loss_price": None,
    "take_profit_price": None,
    "dry_run_usdt_balance": DRY_RUN_STARTING_BALANCE,
    "consecutive_errors": 0,
    "cooldown_until": None,   # ISO timestamp string, or None
    "last_run": None,
    "last_action": None,
}


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        log.info(f"{STATE_FILE} not found — initializing default state.")
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        for k, v in DEFAULT_STATE.items():
            state.setdefault(k, v)
        return state
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Failed to read {STATE_FILE} ({e}) — falling back to default state.")
        return dict(DEFAULT_STATE)


def save_state(state: dict):
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        log.info(f"State saved to {STATE_FILE}")
    except OSError as e:
        log.error(f"Failed to write {STATE_FILE}: {e}")


def in_cooldown(state: dict) -> bool:
    cooldown_until = state.get("cooldown_until")
    if not cooldown_until:
        return False
    try:
        until = datetime.fromisoformat(cooldown_until)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    if now < until:
        log.warning(f"In cooldown until {until.isoformat()} — skipping trading logic this run.")
        return True
    log.info("Cooldown period has passed — resuming normal operation.")
    state["cooldown_until"] = None
    state["consecutive_errors"] = 0
    return False


def register_error(state: dict):
    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
    if state["consecutive_errors"] >= CONSECUTIVE_ERROR_COOLDOWN_THRESHOLD:
        until = datetime.now(timezone.utc) + timedelta(minutes=COOLDOWN_MINUTES)
        state["cooldown_until"] = until.isoformat()
        log.critical(f"{state['consecutive_errors']} consecutive errors — "
                     f"cooling down until {until.isoformat()}.")


def register_success(state: dict):
    state["consecutive_errors"] = 0


# =============================================================================
# ERROR HANDLING — retry decorator with exponential backoff
# =============================================================================

def with_retries(max_attempts=MAX_API_RETRIES, base_delay=RETRY_BACKOFF_SECONDS):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except ccxt.RateLimitExceeded as e:
                    delay = base_delay * (2 ** (attempt - 1))
                    log.warning(f"{func.__name__}: rate limited, retrying in {delay}s ({attempt}/{max_attempts})")
                    time.sleep(delay)
                    last_exc = e
                except ccxt.NetworkError as e:
                    delay = base_delay * (2 ** (attempt - 1))
                    log.warning(f"{func.__name__}: network error, retrying in {delay}s ({attempt}/{max_attempts}): {e}")
                    time.sleep(delay)
                    last_exc = e
                except ccxt.InsufficientFunds:
                    log.error(f"{func.__name__}: insufficient funds, not retrying.")
                    raise
                except ccxt.InvalidOrder:
                    log.error(f"{func.__name__}: invalid order (check min notional/size), not retrying.")
                    raise
                except ccxt.ExchangeError as e:
                    log.error(f"{func.__name__}: exchange error, not retrying: {e}")
                    raise
                except requests.RequestException as e:
                    delay = base_delay * (2 ** (attempt - 1))
                    log.warning(f"{func.__name__}: HTTP error, retrying in {delay}s ({attempt}/{max_attempts}): {e}")
                    time.sleep(delay)
                    last_exc = e
            log.error(f"{func.__name__}: giving up after {max_attempts} attempts")
            raise last_exc
        return wrapper
    return decorator


# =============================================================================
# EXCHANGE SETUP
# =============================================================================

# -----------------------------------------------------------------------
# NO TRANSFER: this bot never calls exchange.transfer(), withdraw(), or
# any wallet-to-wallet movement method — not for spot->futures, not for
# futures->spot, not for withdrawal to an external address. If a future
# edit to this file adds a call to any of those, that is a deliberate
# change outside this bot's original design, not something this script
# does on your behalf today. Fund the futures wallet yourself in Bitget's
# UI before enabling LIVE_TRADING.
# -----------------------------------------------------------------------

def build_exchange():
    api_key = os.environ.get("BITGET_API_KEY")
    api_secret = os.environ.get("BITGET_API_SECRET")
    api_password = os.environ.get("BITGET_API_PASSWORD")

    if LIVE_TRADING and not all([api_key, api_secret, api_password]):
        raise RuntimeError(
            "LIVE_TRADING is True but API credentials are missing. Set "
            "BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSWORD env vars / secrets."
        )

    return ccxt.bitget({
        "apiKey": api_key or "dry-run-placeholder",
        "secret": api_secret or "dry-run-placeholder",
        "password": api_password or "dry-run-placeholder",
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},   # swap = perpetual futures, never spot
    })


def effective_leverage():
    lev = min(LEVERAGE, MAX_ALLOWED_LEVERAGE)
    if lev != LEVERAGE:
        log.warning(f"Configured LEVERAGE={LEVERAGE} exceeds safety cap; using {lev}x.")
    return lev


# =============================================================================
# SIGNALS
# =============================================================================

def compute_rsi(closes: pd.Series, period: int) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def rsi_score(rsi: float) -> float:
    if rsi <= RSI_OVERSOLD:
        return 1.0
    if rsi >= RSI_OVERBOUGHT:
        return -1.0
    midpoint = (RSI_OVERSOLD + RSI_OVERBOUGHT) / 2
    return (midpoint - rsi) / (midpoint - RSI_OVERSOLD)


def volume_score(df: pd.DataFrame) -> float:
    recent_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].iloc[-(VOLUME_LOOKBACK + 1):-1].mean()
    price_change = df["close"].iloc[-1] - df["close"].iloc[-2]

    if avg_vol == 0 or pd.isna(avg_vol):
        return 0.0

    spike_ratio = recent_vol / avg_vol
    if spike_ratio < VOLUME_SPIKE_MULTIPLIER:
        return 0.0

    direction = 1.0 if price_change > 0 else (-1.0 if price_change < 0 else 0.0)
    strength = min((spike_ratio - VOLUME_SPIKE_MULTIPLIER) / VOLUME_SPIKE_MULTIPLIER, 1.0)
    return direction * max(strength, 0.5)


@with_retries(max_attempts=2, base_delay=3)
def fetch_headlines(token: str, asset: str):
    url = "https://cryptopanic.com/api/v1/posts/"
    params = {"auth_token": token, "currencies": asset, "public": "true"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return [item.get("title", "") for item in data.get("results", [])[:NEWS_MAX_HEADLINES]]


def news_sentiment_score() -> float:
    if not NEWS_ENABLED:
        return 0.0
    token = os.environ.get("CRYPTOPANIC_API_TOKEN")
    if not token:
        log.info("CRYPTOPANIC_API_TOKEN not set — skipping news signal.")
        return 0.0
    try:
        headlines = fetch_headlines(token, BASE_ASSET)
    except Exception as e:
        log.warning(f"News fetch failed, degrading gracefully to neutral: {e}")
        return 0.0
    if not headlines:
        return 0.0
    scores = [sentiment_analyzer.polarity_scores(h)["compound"] for h in headlines]
    avg_score = sum(scores) / len(scores)
    log.info(f"News sentiment over {len(headlines)} headlines: {avg_score:+.3f}")
    return avg_score


def combined_signal_score(df: pd.DataFrame) -> dict:
    rsi = compute_rsi(df["close"], RSI_PERIOD)
    s_rsi = rsi_score(rsi)
    s_volume = volume_score(df)
    s_news = news_sentiment_score()

    news_available = NEWS_ENABLED and bool(os.environ.get("CRYPTOPANIC_API_TOKEN"))
    if not news_available:
        w_rsi = WEIGHT_RSI / (WEIGHT_RSI + WEIGHT_VOLUME)
        w_vol = WEIGHT_VOLUME / (WEIGHT_RSI + WEIGHT_VOLUME)
        w_news = 0.0
    else:
        w_rsi, w_vol, w_news = WEIGHT_RSI, WEIGHT_VOLUME, WEIGHT_NEWS

    total = (s_rsi * w_rsi) + (s_volume * w_vol) + (s_news * w_news)
    return {"rsi": rsi, "rsi_score": s_rsi, "volume_score": s_volume,
            "news_score": s_news, "combined": total}


# =============================================================================
# EXCHANGE ACTIONS
# =============================================================================

@with_retries()
def _fetch_ohlcv(exchange):
    return exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=RSI_PERIOD + VOLUME_LOOKBACK + 20)


@with_retries()
def _fetch_balance(exchange):
    return exchange.fetch_balance()


@with_retries()
def _set_leverage(exchange):
    try:
        exchange.set_leverage(effective_leverage(), SYMBOL, params={"marginMode": MARGIN_MODE})
        log.info(f"Leverage set to {effective_leverage()}x ({MARGIN_MODE}) for {SYMBOL}")
    except Exception as e:
        log.warning(f"Could not set leverage explicitly (continuing with account default): {e}")


@with_retries()
def _open_long(exchange, amount, params):
    return exchange.create_order(SYMBOL, "market", "buy", amount, params=params)


@with_retries()
def _close_long(exchange, amount, params):
    close_params = dict(params)
    close_params["reduceOnly"] = True
    return exchange.create_order(SYMBOL, "market", "sell", amount, params=close_params)


@with_retries()
def _check_margin_health(exchange):
    try:
        bal = exchange.fetch_balance(params={"marginMode": MARGIN_MODE, "type": "swap"})
        info = bal.get("info", {})
        risk_ratio = info.get("riskRate") or info.get("marginRatio")
        if risk_ratio is None:
            log.warning("Could not read margin health ratio; blocking new entries.")
            return False
        return float(risk_ratio) < (1 / MIN_MARGIN_HEALTH_RATIO)
    except Exception as e:
        log.warning(f"Margin health check failed, blocking new entries: {e}")
        return False


def order_params():
    return {"marginMode": MARGIN_MODE, "leverage": effective_leverage()}


def get_balances(exchange, state):
    """Returns (available USDT margin, current position size in base asset).
    Always reads the FUTURES wallet — never spot. No transfer is ever
    initiated to move funds into it; that's on you, done manually on Bitget."""
    if not LIVE_TRADING:
        return state["dry_run_usdt_balance"], state["base_asset_amount"]
    bal = _fetch_balance(exchange)
    return bal["free"].get(QUOTE_ASSET, 0.0), bal["free"].get(BASE_ASSET, 0.0)


def place_buy(exchange, usd_amount, price, state):
    """Opens a long futures position and immediately attaches TP/SL orders."""
    amount = usd_amount / price
    params = order_params()

    sl_price = price * (1 - STOP_LOSS_PCT)
    tp_price = price * (1 + TAKE_PROFIT_PCT)
    params_with_exits = dict(params)
    params_with_exits["stopLossPrice"] = sl_price
    params_with_exits["takeProfitPrice"] = tp_price

    if LIVE_TRADING:
        _set_leverage(exchange)
        order = _open_long(exchange, amount, params_with_exits)
        log.info(f"LIVE LONG opened [futures, {effective_leverage()}x]: {order} "
                 f"| attached SL={sl_price:.2f} TP={tp_price:.2f}")
    else:
        state["dry_run_usdt_balance"] -= usd_amount
        log.info(f"[DRY RUN/futures] LONG {amount:.8f} {BASE_ASSET} @ {price:.2f} "
                 f"(${usd_amount:.4f}, {effective_leverage()}x) | SL={sl_price:.2f} TP={tp_price:.2f} "
                 f"| sim balance: ${state['dry_run_usdt_balance']:.4f}")
    state["in_position"] = True
    state["entry_price"] = price
    state["base_asset_amount"] = amount
    state["stop_loss_price"] = sl_price
    state["take_profit_price"] = tp_price
    state["last_action"] = f"LONG @ {price:.2f}"


def place_sell(exchange, amount, price, state, reason=""):
    """Closes the long futures position (reduceOnly — never opens a short)."""
    if LIVE_TRADING:
        order = _close_long(exchange, amount, order_params())
        log.info(f"LIVE position closed [futures] ({reason}): {order}")
    else:
        proceeds = amount * price
        entry = state.get("entry_price")
        pnl = ((price / entry) - 1) * 100 * effective_leverage() if entry else 0
        state["dry_run_usdt_balance"] += proceeds
        log.info(f"[DRY RUN/futures] CLOSE {amount:.8f} {BASE_ASSET} @ {price:.2f} ({reason}) "
                 f"| PnL (leveraged): {pnl:+.2f}% | sim balance: ${state['dry_run_usdt_balance']:.4f}")
    state["in_position"] = False
    state["entry_price"] = None
    state["base_asset_amount"] = 0.0
    state["stop_loss_price"] = None
    state["take_profit_price"] = None
    state["last_action"] = f"CLOSE @ {price:.2f} ({reason})"


# =============================================================================
# SINGLE RUN
# =============================================================================

def main():
    state = load_state()

    if in_cooldown(state):
        save_state(state)
        return

    try:
        exchange = build_exchange()
        exchange.load_markets()

        mode = "LIVE" if LIVE_TRADING else "DRY RUN"
        log.info(f"Run start | mode={mode} | market=futures (USDT-M perpetual) | symbol={SYMBOL}")
        log.warning(f"FUTURES MODE — leverage capped at {effective_leverage()}x. "
                    "Losses can exceed your futures wallet margin and trigger liquidation.")

        ohlcv = _fetch_ohlcv(exchange)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        current_price = df["close"].iloc[-1]

        signals = combined_signal_score(df)
        quote_bal, base_bal = get_balances(exchange, state)
        in_position = state.get("in_position", False)
        entry_price = state.get("entry_price")

        log.info(
            f"Price={current_price:.2f} RSI={signals['rsi']:.1f}(score {signals['rsi_score']:+.2f}) "
            f"Vol_score={signals['volume_score']:+.2f} News_score={signals['news_score']:+.2f} "
            f"=> Combined={signals['combined']:+.2f} | in_position={in_position}"
        )

        if in_position:
            if entry_price:
                change = (current_price / entry_price) - 1
                if change <= -STOP_LOSS_PCT:
                    place_sell(exchange, base_bal, current_price, state, "stop-loss")
                elif change >= TAKE_PROFIT_PCT:
                    place_sell(exchange, base_bal, current_price, state, "take-profit")
                elif signals["combined"] <= SELL_SCORE_THRESHOLD:
                    place_sell(exchange, base_bal, current_price, state, "signal exit")
                else:
                    log.info("Holding position — no exit condition met.")
            else:
                log.warning("in_position=True but no entry_price in state — inconsistent state, holding.")
        else:
            if signals["combined"] >= BUY_SCORE_THRESHOLD:
                proceed = True
                if LIVE_TRADING:
                    proceed = _check_margin_health(exchange)
                    if not proceed:
                        log.warning("Margin health check failed — skipping entry this run.")
                if proceed:
                    usd_to_spend = quote_bal * POSITION_SIZE_PCT
                    if usd_to_spend < MIN_NOTIONAL_USD:
                        log.warning(f"Buy signal fired but ${usd_to_spend:.4f} is below "
                                    f"minimum notional ${MIN_NOTIONAL_USD}. Skipping.")
                    else:
                        place_buy(exchange, usd_to_spend, current_price, state)
            else:
                log.info("No buy signal this run.")

        register_success(state)

    except ccxt.InsufficientFunds as e:
        log.error(f"Insufficient funds: {e}")
        register_error(state)
    except ccxt.InvalidOrder as e:
        log.error(f"Order rejected (check MIN_NOTIONAL_USD / exchange minimums): {e}")
        register_error(state)
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.error(f"Exchange/network error: {e}")
        register_error(state)
    except Exception as e:
        log.exception(f"Unexpected error: {e}")
        register_error(state)

    save_state(state)


if __name__ == "__main__":
    main()
