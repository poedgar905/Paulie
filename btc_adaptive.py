"""
btc_adaptive.py — Adaptive 15-Minute BTC Bot

Self-adjusting bot that analyzes market conditions each period
and picks the optimal strategy automatically.

No manual settings needed — just /15min_bot to start.

See ALGORITHM.md for full documentation.
"""
import asyncio
import json
import logging
import time
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────
_bot_active = False
_bot_stats = {
    "wins": 0, "losses": 0, "total_pnl": 0.0,
    "total_trades": 0, "started_at": 0,
    "current_slug": "", "current_entered": False,
}

# Track recent results for adaptive behavior
_recent_results: list[dict] = []  # last 20 trades


# ── Binance Data ─────────────────────────────────────────

def _get_btc_price() -> float | None:
    import requests
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=5)
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception:
        pass
    return None


def _get_btc_kline(interval: str = "15m", limit: int = 1) -> dict | None:
    import requests
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data:
                k = data[-1]
                return {
                    "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]),
                    "volume": float(k[5]), "num_trades": int(k[8]),
                    "taker_buy_vol": float(k[9]),
                }
    except Exception:
        pass
    return None


def _get_1m_candles(limit: int = 5) -> list[dict] | None:
    """Get last N 1-minute candles."""
    import requests
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
            timeout=5)
        if resp.status_code == 200:
            return [
                {
                    "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]),
                    "volume": float(k[5]), "num_trades": int(k[8]),
                    "taker_buy_vol": float(k[9]),
                }
                for k in resp.json()
            ]
    except Exception:
        pass
    return None


# ── Market Analysis ──────────────────────────────────────

@dataclass
class MarketAnalysis:
    """Complete analysis of current 15m period."""
    # Price data
    btc_open: float = 0
    btc_now: float = 0
    btc_change: float = 0
    btc_change_pct: float = 0
    direction: str = ""  # "Up" or "Down"

    # Trend strength (0-100)
    trend_score: int = 0

    # Volatility
    avg_1m_range: float = 0
    last_1m_range: float = 0
    volatility_ratio: float = 0  # last vs avg

    # Volume analysis
    avg_volume: float = 0
    last_volume: float = 0
    volume_ratio: float = 0
    buy_ratio: float = 0.5  # taker buy / total

    # Momentum (are last candles confirming direction?)
    momentum_score: int = 0  # -100 to +100
    consecutive_candles: int = 0  # how many 1m candles in same direction

    # Risk flags
    spike_detected: bool = False
    reversal_risk: bool = False
    low_confidence: bool = False

    # Decision
    strategy: str = ""  # "confident", "moderate", "skip"
    entry_price: float = 0
    reason: str = ""


def analyze_market(time_left: int) -> MarketAnalysis:
    """Analyze current BTC market conditions."""
    a = MarketAnalysis()

    # Get period kline (15m)
    kline = _get_btc_kline("15m", 1)
    if not kline:
        a.reason = "No kline data"
        a.strategy = "skip"
        return a

    btc_now = _get_btc_price()
    if not btc_now:
        a.reason = "No BTC price"
        a.strategy = "skip"
        return a

    a.btc_open = kline["open"]
    a.btc_now = btc_now
    a.btc_change = btc_now - kline["open"]
    a.btc_change_pct = abs(a.btc_change / a.btc_open) * 100 if a.btc_open else 0
    a.direction = "Up" if a.btc_change > 0 else "Down"

    # ── 1-minute candle analysis ──────────────────────
    candles = _get_1m_candles(8)
    if not candles or len(candles) < 4:
        a.reason = "Not enough candle data"
        a.strategy = "skip"
        return a

    prev_candles = candles[:-1]
    last = candles[-1]

    # Volatility
    ranges = [c["high"] - c["low"] for c in prev_candles]
    a.avg_1m_range = sum(ranges) / len(ranges) if ranges else 1
    a.last_1m_range = last["high"] - last["low"]
    a.volatility_ratio = a.last_1m_range / a.avg_1m_range if a.avg_1m_range > 0 else 1

    # Volume
    volumes = [c["volume"] for c in prev_candles]
    a.avg_volume = sum(volumes) / len(volumes) if volumes else 1
    a.last_volume = last["volume"]
    a.volume_ratio = a.last_volume / a.avg_volume if a.avg_volume > 0 else 1

    # Buy ratio
    a.buy_ratio = last["taker_buy_vol"] / last["volume"] if last["volume"] > 0 else 0.5

    # Momentum — count consecutive 1m candles in same direction
    consecutive = 0
    momentum_sum = 0
    for c in reversed(candles[-5:]):
        move = c["close"] - c["open"]
        if a.btc_change > 0 and move > 0:
            consecutive += 1
            momentum_sum += move
        elif a.btc_change < 0 and move < 0:
            consecutive += 1
            momentum_sum += abs(move)
        else:
            break
    a.consecutive_candles = consecutive
    a.momentum_score = min(consecutive * 20, 100)

    # Trend score (0-100): combines move size + momentum + consistency
    move_score = min(a.btc_change_pct * 200, 40)  # max 40 pts from move size
    momentum_pts = min(consecutive * 15, 30)       # max 30 pts from momentum
    # Consistency: what % of candles agree with direction
    agreeing = sum(1 for c in candles[-5:] if
                   (c["close"] - c["open"] > 0) == (a.btc_change > 0))
    consistency_pts = (agreeing / 5) * 30           # max 30 pts
    a.trend_score = int(move_score + momentum_pts + consistency_pts)

    # ── Risk detection ────────────────────────────────
    # Spike: volume 3x+ AND range 3x+
    if a.volume_ratio > 3 and a.volatility_ratio > 3:
        a.spike_detected = True

    # Volume spike with opposing buy ratio
    if a.volume_ratio > 3:
        if a.btc_change < 0 and a.buy_ratio > 0.85:
            a.spike_detected = True  # Selling but whales buying
        elif a.btc_change > 0 and a.buy_ratio < 0.15:
            a.spike_detected = True  # Buying but whales selling

    # Reversal risk: last candle opposite to overall direction
    last_move = last["close"] - last["open"]
    if a.btc_change > 0 and last_move < 0:
        retrace = abs(last_move / a.btc_change) if a.btc_change != 0 else 0
        if retrace > 0.4:
            a.reversal_risk = True
    elif a.btc_change < 0 and last_move > 0:
        retrace = abs(last_move / a.btc_change) if a.btc_change != 0 else 0
        if retrace > 0.4:
            a.reversal_risk = True

    # Low confidence: tiny move
    if a.btc_change_pct < 0.03:
        a.low_confidence = True

    # ── Strategy decision ─────────────────────────────
    if a.spike_detected:
        a.strategy = "skip"
        a.reason = f"Spike detected (vol {a.volume_ratio:.1f}x, range {a.volatility_ratio:.1f}x)"
        return a

    if a.low_confidence:
        a.strategy = "skip"
        a.reason = f"BTC move too small ({a.btc_change_pct:.3f}%)"
        return a

    # === CONFIDENT: Strong trend, enter high ===
    # BTC moved > 0.12%, 3+ consecutive candles, trend score > 70
    if (a.btc_change_pct > 0.12 and a.consecutive_candles >= 3
            and a.trend_score > 70 and not a.reversal_risk
            and time_left <= 45):
        a.strategy = "confident"
        a.entry_price = 0.88
        a.reason = (f"Strong trend: {a.btc_change_pct:.2f}%, "
                    f"score={a.trend_score}, {a.consecutive_candles} candles, "
                    f"momentum={a.momentum_score}")
        return a

    # === MODERATE: Decent trend, enter mid ===
    # BTC moved > 0.06%, 2+ candles, score > 45, not reversing
    if (a.btc_change_pct > 0.06 and a.consecutive_candles >= 2
            and a.trend_score > 45 and not a.reversal_risk
            and time_left <= 90):
        a.strategy = "moderate"
        a.entry_price = 0.70
        a.reason = (f"Moderate trend: {a.btc_change_pct:.2f}%, "
                    f"score={a.trend_score}, {a.consecutive_candles} candles")
        return a

    # === EARLY: Weak but consistent, enter cheap ===
    # BTC moved > 0.04%, at least some consistency
    if (a.btc_change_pct > 0.04 and a.trend_score > 30
            and not a.reversal_risk and time_left <= 150):
        a.strategy = "early"
        a.entry_price = 0.58
        a.reason = (f"Early entry: {a.btc_change_pct:.2f}%, "
                    f"score={a.trend_score}, cheap entry")
        return a

    # === DEFAULT: Skip ===
    a.strategy = "skip"
    a.reason = (f"No clear signal: move={a.btc_change_pct:.3f}%, "
                f"score={a.trend_score}, candles={a.consecutive_candles}, "
                f"time_left={time_left}s")
    return a


# ── Polymarket helpers ───────────────────────────────────

def _find_live_market():
    """Find current 15m BTC market."""
    from sniper import find_live_market
    return find_live_market("15m")


def _fetch_midprice(token_id: str) -> float | None:
    from sniper import fetch_midprice
    return fetch_midprice(token_id)


# ── Notifications ────────────────────────────────────────

async def _notify(bot, text: str):
    from config import OWNER_ID, CHANNEL_ID
    for chat_id in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            pass


# ── Google Sheets ────────────────────────────────────────

def _log_to_sheets(data: dict):
    """Log trade to '🤖 Adaptive' sheet."""
    import threading
    def _write():
        try:
            from sheets import _get_client, _get_or_create_sheet
            from datetime import datetime, timezone

            gc, spreadsheet = _get_client()
            if not gc or not spreadsheet:
                return

            ws = _get_or_create_sheet(spreadsheet, "🤖 Adaptive")

            try:
                first_cell = ws.acell("A1").value
            except Exception:
                first_cell = None

            if not first_cell:
                headers = [
                    "Timestamp", "Market", "Strategy", "Direction",
                    "Entry (¢)", "BTC Move (%)", "Trend Score",
                    "Consec. Candles", "Vol Ratio", "Buy Ratio",
                    "Result", "P&L ($)", "Shares", "Cost ($)",
                    "Reason",
                ]
                ws.update("A1:O1", [headers], value_input_option="USER_ENTERED")

                summary = [
                    ["ADAPTIVE STATS", ""],
                    ["Total trades", '=COUNTA(A2:A)'],
                    ["Wins", '=COUNTIF(K2:K,"WIN")'],
                    ["Losses", '=COUNTIF(K2:K,"LOSS")'],
                    ["Win Rate %", '=IF(Q3>0,Q4/(Q4+Q5)*100,0)'],
                    ["Total P&L", '=SUM(L2:L)'],
                    ["Avg Win $", '=IFERROR(AVERAGEIF(K2:K,"WIN",L2:L),0)'],
                    ["Avg Loss $", '=IFERROR(AVERAGEIF(K2:K,"LOSS",L2:L),0)'],
                    ["", ""],
                    ["CONFIDENT WR%", '=IFERROR(COUNTIFS(C2:C,"confident",K2:K,"WIN")/COUNTIF(C2:C,"confident")*100,0)'],
                    ["MODERATE WR%", '=IFERROR(COUNTIFS(C2:C,"moderate",K2:K,"WIN")/COUNTIF(C2:C,"moderate")*100,0)'],
                    ["EARLY WR%", '=IFERROR(COUNTIFS(C2:C,"early",K2:K,"WIN")/COUNTIF(C2:C,"early")*100,0)'],
                ]
                ws.update("Q1:R12", summary, value_input_option="USER_ENTERED")

                try:
                    ws.format("A1:O1", {"textFormat": {"bold": True}})
                    ws.format("Q1:Q1", {"textFormat": {"bold": True}})
                except Exception:
                    pass

            row = [
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                data.get("market", ""),
                data.get("strategy", ""),
                data.get("direction", ""),
                data.get("entry_price", 0),
                data.get("btc_move_pct", 0),
                data.get("trend_score", 0),
                data.get("consecutive", 0),
                data.get("vol_ratio", 0),
                data.get("buy_ratio", 0),
                data.get("result", ""),
                data.get("pnl", 0),
                data.get("shares", 0),
                data.get("cost", 0),
                data.get("reason", ""),
            ]
            ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.error("Adaptive sheets error: %s", e)

    threading.Thread(target=_write, daemon=True).start()


# ── Persistence ──────────────────────────────────────────

def _save_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS adaptive_bot (
            key TEXT PRIMARY KEY, value TEXT)""")
        state = {
            "active": _bot_active,
            "wins": _bot_stats["wins"],
            "losses": _bot_stats["losses"],
            "total_pnl": round(_bot_stats["total_pnl"], 4),
            "total_trades": _bot_stats["total_trades"],
        }
        c.execute("INSERT OR REPLACE INTO adaptive_bot VALUES (?, ?)",
                  ("state", json.dumps(state)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Save adaptive state: %s", e)


def _load_state() -> bool:
    from config import DB_PATH
    global _bot_active
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS adaptive_bot (
            key TEXT PRIMARY KEY, value TEXT)""")
        row = c.execute("SELECT value FROM adaptive_bot WHERE key='state'").fetchone()
        conn.close()
        if row:
            state = json.loads(row[0])
            _bot_active = state.get("active", False)
            _bot_stats["wins"] = state.get("wins", 0)
            _bot_stats["losses"] = state.get("losses", 0)
            _bot_stats["total_pnl"] = state.get("total_pnl", 0)
            _bot_stats["total_trades"] = state.get("total_trades", 0)
            _bot_stats["started_at"] = int(time.time())
            return _bot_active
    except Exception as e:
        logger.error("Load adaptive state: %s", e)
    return False


# ── Active Session Tracking ──────────────────────────────

@dataclass
class ActiveTrade:
    """One active trade in the adaptive bot."""
    slug: str = ""
    condition_id: str = ""
    token_id: str = ""
    order_id: str = ""
    order_status: str = ""  # live, matched
    direction: str = ""
    strategy: str = ""
    entry_price: float = 0
    size_usdc: float = 1.0
    shares: float = 0
    cost: float = 0
    mid_at_fill: float = 0
    market_end_ts: int = 0
    title: str = ""
    analysis: dict = field(default_factory=dict)


_active_trade: ActiveTrade | None = None


# ── Main Loop ────────────────────────────────────────────

async def adaptive_checker(bot):
    """Main loop — every 1.5 seconds."""
    global _bot_active
    logger.info("Adaptive BTC bot checker started (1.5s)")
    await asyncio.sleep(5)

    # Auto-restore
    if _load_state():
        await _notify(bot, "🔄 <b>Adaptive BTC Bot auto-restored</b>")

    while True:
        try:
            if _bot_active:
                await _run_cycle(bot)
        except Exception as e:
            logger.error("Adaptive bot error: %s", e)
        await asyncio.sleep(1.5)


async def _run_cycle(bot):
    """One cycle of the adaptive bot."""
    global _active_trade
    from trading import place_limit_buy, check_order_status, cancel_order

    now = int(time.time())

    # ── Check active trade first ──────────────────────
    if _active_trade:
        await _check_active_trade(bot)
        return

    # ── Find live market ──────────────────────────────
    live = _find_live_market()
    if not live:
        return

    slug = live["slug"]
    end_ts = live["end_ts"]
    time_left = end_ts - now

    if time_left <= 0 or time_left > 900:
        _bot_stats["current_slug"] = ""
        _bot_stats["current_entered"] = False
        return

    # New market?
    if slug != _bot_stats["current_slug"]:
        _bot_stats["current_slug"] = slug
        _bot_stats["current_entered"] = False

    if _bot_stats["current_entered"]:
        return

    # ── Not in entry window yet ───────────────────────
    # We analyze from 150s before close
    if time_left > 150:
        return

    # ── ANALYZE ───────────────────────────────────────
    analysis = analyze_market(time_left)

    if analysis.strategy == "skip":
        # Log skip once per market
        if not _bot_stats.get(f"_skip_{slug}"):
            _bot_stats[f"_skip_{slug}"] = True
            # Only log first skip reason per market (not spamming)
            logger.info("Adaptive SKIP %s: %s", slug[-12:], analysis.reason)
        return

    # ── Time gate per strategy ────────────────────────
    # Confident: enter in last 45s
    # Moderate: enter in last 90s
    # Early: enter in last 150s
    if analysis.strategy == "confident" and time_left > 45:
        return
    if analysis.strategy == "moderate" and time_left > 90:
        return
    if analysis.strategy == "early" and time_left > 150:
        return

    # ── Get token ID ──────────────────────────────────
    if analysis.direction == "Up":
        token_id = live["token_yes"]
    else:
        token_id = live["token_no"]

    if not token_id:
        return

    # ── Check mid price ───────────────────────────────
    mid = _fetch_midprice(token_id)
    if mid and mid > analysis.entry_price:
        # Too expensive for this strategy
        if not _bot_stats.get(f"_exp_{slug}"):
            _bot_stats[f"_exp_{slug}"] = True
            logger.info("Adaptive: mid %.0f¢ > entry %.0f¢ for %s strategy",
                        mid * 100, analysis.entry_price * 100, analysis.strategy)
        return

    # ── ENTER! ────────────────────────────────────────
    _bot_stats["current_entered"] = True
    size = 1.0  # $1 per trade

    result = place_limit_buy(token_id, analysis.entry_price, size,
                             live["condition_id"])
    if not result or not result.get("order_id"):
        await _notify(bot,
            f"⚠️ <b>FAIL</b> | {analysis.strategy.upper()}\n"
            f"{'🟢' if analysis.direction == 'Up' else '🔴'} {analysis.direction}\n"
            f"❌ Order placement failed")
        return

    _active_trade = ActiveTrade(
        slug=slug,
        condition_id=live["condition_id"],
        token_id=token_id,
        order_id=result["order_id"],
        order_status="live",
        direction=analysis.direction,
        strategy=analysis.strategy,
        entry_price=analysis.entry_price,
        size_usdc=size,
        market_end_ts=end_ts,
        title=live["question"],
        analysis={
            "btc_move_pct": round(analysis.btc_change_pct, 4),
            "trend_score": analysis.trend_score,
            "consecutive": analysis.consecutive_candles,
            "vol_ratio": round(analysis.volume_ratio, 1),
            "buy_ratio": round(analysis.buy_ratio, 2),
            "reason": analysis.reason,
        },
    )

    strategy_emoji = {"confident": "🟢", "moderate": "🟡", "early": "🔵"}
    emoji = strategy_emoji.get(analysis.strategy, "⚪")

    await _notify(bot,
        f"{emoji} <b>{analysis.strategy.upper()}</b> | {live['question'][:50]}\n"
        f"{'🟢' if analysis.direction == 'Up' else '🔴'} {analysis.direction} @ {analysis.entry_price*100:.0f}¢\n"
        f"📊 BTC: {analysis.btc_change:+.0f} ({analysis.btc_change_pct:.2f}%)\n"
        f"📈 Score: {analysis.trend_score} | {analysis.consecutive_candles} candles\n"
        f"🔉 Vol: {analysis.volume_ratio:.1f}x | Buy: {analysis.buy_ratio:.0%}\n"
        f"⏱ {time_left}s left\n"
        f"💡 {analysis.reason}"
    )


async def _check_active_trade(bot):
    """Check fill status and resolution of active trade."""
    global _active_trade
    from trading import check_order_status, cancel_order

    if not _active_trade:
        return

    trade = _active_trade
    now = int(time.time())

    # ── Check fill ────────────────────────────────────
    if trade.order_status == "live":
        status = check_order_status(trade.order_id)
        status_lower = (status or "").lower()

        if status_lower == "matched":
            trade.order_status = "matched"
            trade.shares = round(trade.size_usdc / trade.entry_price, 2)
            trade.cost = trade.size_usdc
            mid = _fetch_midprice(trade.token_id)
            trade.mid_at_fill = mid if mid else 0
            return

        # Not filled and market ended?
        if now > trade.market_end_ts + 30:
            try:
                cancel_order(trade.order_id)
            except Exception:
                pass
            _active_trade = None
            return

        return

    # ── Check resolution ──────────────────────────────
    if trade.order_status == "matched":
        if now < trade.market_end_ts + 15:
            return  # Wait for resolution

        # Try API resolution first
        from sniper import find_live_market, get_market_end_timestamp
        import requests

        resolution = None

        # Check via Gamma API
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": trade.slug},
                timeout=10)
            if resp.status_code == 200:
                events = resp.json()
                if events and isinstance(events, list):
                    markets = events[0].get("markets", [])
                    for m in markets:
                        if m.get("conditionId") == trade.condition_id:
                            r = m.get("resolution", "")
                            if r:
                                resolution = r
        except Exception:
            pass

        # BTC fallback after 120s
        if not resolution and now > trade.market_end_ts + 120:
            try:
                market_start_ms = (trade.market_end_ts - 900) * 1000
                resp = requests.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": "BTCUSDT", "interval": "15m",
                            "startTime": market_start_ms, "limit": 1},
                    timeout=5)
                if resp.status_code == 200:
                    candles = resp.json()
                    if candles:
                        btc_open = float(candles[0][1])
                        btc_close = float(candles[0][4])
                        resolution = "p1" if btc_close > btc_open else "p2"
            except Exception:
                pass

        if not resolution:
            # Timeout after 600s
            if now > trade.market_end_ts + 600:
                resolution = "timeout"
            else:
                return

        # ── Determine WIN/LOSS ────────────────────────
        won = False
        if resolution in ("p1", "Yes", "1"):
            won = (trade.direction == "Up")
        elif resolution in ("p2", "No", "0"):
            won = (trade.direction == "Down")

        if won:
            pnl = trade.shares * 1.0 - trade.cost
            result = "WIN"
        else:
            pnl = -trade.cost
            result = "LOSS"

        # Update stats
        _bot_stats["total_trades"] += 1
        if won:
            _bot_stats["wins"] += 1
        else:
            _bot_stats["losses"] += 1
        _bot_stats["total_pnl"] += pnl
        _save_state()

        # Track for adaptive learning
        _recent_results.append({
            "strategy": trade.strategy, "result": result,
            "pnl": pnl, "ts": now,
        })
        if len(_recent_results) > 20:
            _recent_results.pop(0)

        # Log
        emoji = "🟩" if won else "🟥"
        sign = "+" if pnl >= 0 else ""
        w = _bot_stats["wins"]
        l = _bot_stats["losses"]
        total = w + l
        wr = (w / total * 100) if total > 0 else 0
        total_sign = "+" if _bot_stats["total_pnl"] >= 0 else ""

        await _notify(bot,
            f"{emoji} <b>{result}</b> | {trade.strategy.upper()} | {trade.title[:40]}\n"
            f"{'🟢' if trade.direction == 'Up' else '🔴'} {trade.direction} @ {trade.entry_price*100:.0f}¢\n"
            f"💰 {sign}${pnl:.2f} ({trade.shares} shares)\n"
            f"📈 {w}W/{l}L ({wr:.0f}%) | Total: {total_sign}${_bot_stats['total_pnl']:.2f}"
        )

        # Log to sheets
        _log_to_sheets({
            "market": trade.title[:50],
            "strategy": trade.strategy,
            "direction": trade.direction,
            "entry_price": round(trade.entry_price * 100, 1),
            "btc_move_pct": trade.analysis.get("btc_move_pct", 0),
            "trend_score": trade.analysis.get("trend_score", 0),
            "consecutive": trade.analysis.get("consecutive", 0),
            "vol_ratio": trade.analysis.get("vol_ratio", 0),
            "buy_ratio": trade.analysis.get("buy_ratio", 0),
            "result": result,
            "pnl": round(pnl, 4),
            "shares": trade.shares,
            "cost": trade.cost,
            "reason": trade.analysis.get("reason", ""),
        })

        _active_trade = None


# ── Control ──────────────────────────────────────────────

def start_adaptive():
    global _bot_active
    _bot_active = True
    _bot_stats["started_at"] = int(time.time())
    _save_state()


def stop_adaptive():
    global _bot_active, _active_trade
    _bot_active = False
    if _active_trade and _active_trade.order_status == "live":
        from trading import cancel_order
        try:
            cancel_order(_active_trade.order_id)
        except Exception:
            pass
    _active_trade = None
    _save_state()


def is_active() -> bool:
    return _bot_active


def get_status() -> str:
    if not _bot_active:
        return "🤖 Adaptive BTC Bot: OFF"

    w = _bot_stats["wins"]
    l = _bot_stats["losses"]
    total = w + l
    wr = (w / total * 100) if total > 0 else 0
    sign = "+" if _bot_stats["total_pnl"] >= 0 else ""
    runtime = int(time.time()) - _bot_stats.get("started_at", int(time.time()))
    hours = runtime // 3600

    trade_text = ""
    if _active_trade:
        t = _active_trade
        emoji = {"confident": "🟢", "moderate": "🟡", "early": "🔵"}.get(t.strategy, "⚪")
        trade_text = (
            f"\n\n🔥 <b>Active:</b>\n"
            f"  {emoji} {t.strategy.upper()} | {t.direction} @ {t.entry_price*100:.0f}¢\n"
            f"  📌 {t.title[:40]}\n"
            f"  Status: {t.order_status}"
        )

    # Recent strategy breakdown
    strat_text = ""
    if _recent_results:
        for strat in ["confident", "moderate", "early"]:
            trades = [r for r in _recent_results if r["strategy"] == strat]
            if trades:
                wins = sum(1 for r in trades if r["result"] == "WIN")
                strat_text += f"\n  • {strat}: {wins}/{len(trades)} ({wins/len(trades)*100:.0f}%)"

    return (
        f"🤖 <b>Adaptive BTC Bot 🟢 ON</b>\n\n"
        f"📈 {total}T | {w}W/{l}L ({wr:.0f}%)\n"
        f"💰 P&L: {sign}${_bot_stats['total_pnl']:.2f}\n"
        f"⏱ {hours}h"
        + (f"\n\n📊 <b>Recent (last 20):</b>{strat_text}" if strat_text else "")
        + trade_text
    )
