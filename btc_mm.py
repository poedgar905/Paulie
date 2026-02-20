"""
btc_mm.py — Mini Market Maker for BTC Up/Down Markets

Strategy:
  1. Wait for flat + volatile period (no trend, but price jumping)
  2. Buy YES @ 50¢ + NO @ 50¢
  3. Place sell limits: YES @ 60¢, NO @ 60¢
  4. One side sells → profit +10¢
  5. Other side → stop loss at 40¢ (loss capped at -10¢)
  6. Net: ~0¢ to +10¢ per cycle (minus fees)

Entry conditions (via Binance):
  - BTC change < 0.04% (no clear trend)
  - 1m volatility exists (range > $15 avg)
  - At least 3 minutes into the period (market settled)
  - Before minute 10 (enough time for oscillation)

Command: /mm_bot [start|stop|status]
"""
import asyncio
import json
import logging
import time
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────
_mm_active = False
_mm_config = {
    "market_type": "15m",      # 15m or 5m
    "buy_price": 0.50,         # buy both sides at 50¢
    "sell_target": 0.60,       # sell limit at 60¢ (+10¢ profit)
    "stop_loss": 0.40,         # stop loss at 40¢ (-10¢ max loss)
    "size_usdc": 1.0,          # $1 per side
    "min_volatility": 15,      # min avg 1m range in $ to enter
    "max_trend": 0.04,         # max BTC % change (above = trending, skip)
}
_mm_stats = {
    "wins": 0, "losses": 0, "breakeven": 0,
    "total_pnl": 0.0, "total_trades": 0,
    "started_at": 0,
}


# ── Active Session ───────────────────────────────────────

@dataclass
class MMSession:
    """One market maker session."""
    slug: str = ""
    condition_id: str = ""
    title: str = ""
    market_end_ts: int = 0

    # YES side
    yes_token: str = ""
    yes_buy_order: str = ""
    yes_buy_status: str = ""  # live, matched
    yes_sell_order: str = ""
    yes_sell_status: str = ""  # live, matched
    yes_shares: float = 0
    yes_closed: bool = False
    yes_pnl: float = 0

    # NO side
    no_token: str = ""
    no_buy_order: str = ""
    no_buy_status: str = ""
    no_sell_order: str = ""
    no_sell_status: str = ""
    no_shares: float = 0
    no_closed: bool = False
    no_pnl: float = 0

    # State
    phase: str = "buying"  # buying → selling → monitoring → done
    entered_at: int = 0
    reason: str = ""


_active_session: MMSession | None = None


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


def _get_1m_candles(limit: int = 8) -> list[dict] | None:
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
                }
                for k in resp.json()
            ]
    except Exception:
        pass
    return None


def _get_btc_kline_15m() -> dict | None:
    import requests
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1},
            timeout=5)
        if resp.status_code == 200:
            k = resp.json()[-1]
            return {"open": float(k[1]), "close": float(k[4])}
    except Exception:
        pass
    return None


# ── Market Condition Check ───────────────────────────────

@dataclass
class MMCondition:
    """Market conditions for MM entry."""
    btc_change_pct: float = 0
    avg_1m_range: float = 0
    is_flat: bool = False
    is_volatile: bool = False
    should_enter: bool = False
    reason: str = ""


def check_mm_conditions() -> MMCondition:
    """Check if conditions are right for market making."""
    c = MMCondition()

    # Overall trend
    kline = _get_btc_kline_15m()
    if not kline:
        c.reason = "No kline data"
        return c

    btc_now = _get_btc_price()
    if not btc_now:
        c.reason = "No BTC price"
        return c

    change = btc_now - kline["open"]
    c.btc_change_pct = abs(change / kline["open"]) * 100 if kline["open"] else 0

    # 1m candles for volatility
    candles = _get_1m_candles(8)
    if not candles or len(candles) < 4:
        c.reason = "Not enough candles"
        return c

    # Average 1m range (volatility measure)
    ranges = [cd["high"] - cd["low"] for cd in candles]
    c.avg_1m_range = sum(ranges) / len(ranges) if ranges else 0

    # Direction consistency — how many candles agree
    up_candles = sum(1 for cd in candles if cd["close"] > cd["open"])
    down_candles = len(candles) - up_candles
    # If split roughly 50/50 = flat market
    direction_ratio = max(up_candles, down_candles) / len(candles)

    # Flat = low trend + mixed direction
    c.is_flat = (c.btc_change_pct < _mm_config["max_trend"]
                 and direction_ratio < 0.75)

    # Volatile = price actually moving within candles
    c.is_volatile = c.avg_1m_range >= _mm_config["min_volatility"]

    if c.is_flat and c.is_volatile:
        c.should_enter = True
        c.reason = (f"Flat ({c.btc_change_pct:.3f}% < {_mm_config['max_trend']}%) "
                    f"+ Volatile (${c.avg_1m_range:.0f} avg range)")
    elif not c.is_flat:
        c.reason = f"Trending ({c.btc_change_pct:.3f}% > {_mm_config['max_trend']}%)"
    elif not c.is_volatile:
        c.reason = f"Not volatile (${c.avg_1m_range:.0f} < ${_mm_config['min_volatility']})"

    return c


# ── Notifications ────────────────────────────────────────

async def _mm_notify(bot, text: str):
    from config import OWNER_ID, CHANNEL_ID
    for chat_id in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception:
            pass


# ── Main Loop ────────────────────────────────────────────

async def mm_checker(bot):
    """Background loop — every 2 seconds."""
    logger.info("MM bot checker started (2s)")
    await asyncio.sleep(8)

    if _load_mm_state():
        await _mm_notify(bot, "🔄 <b>MM Bot auto-restored</b>")

    while True:
        try:
            if _mm_active:
                await _run_mm_cycle(bot)
        except Exception as e:
            logger.error("MM bot error: %s", e)
        await asyncio.sleep(2)


async def _run_mm_cycle(bot):
    """One cycle of the market maker."""
    global _active_session
    from trading import (place_limit_buy, place_limit_sell, place_market_sell,
                         check_order_status, cancel_order)
    from sniper import find_live_market, fetch_midprice

    now = int(time.time())

    # ── Active session? Handle it ─────────────────────
    if _active_session:
        await _manage_session(bot)
        return

    # ── Find live market ──────────────────────────────
    live = find_live_market(_mm_config["market_type"])
    if not live:
        return

    slug = live["slug"]
    end_ts = live["end_ts"]
    time_left = end_ts - now
    time_elapsed = 900 - time_left if _mm_config["market_type"] == "15m" else 300 - time_left

    # Only enter between minute 3-10 of 15m market (180s-600s elapsed)
    if _mm_config["market_type"] == "15m":
        if time_elapsed < 180 or time_elapsed > 600:
            return
    else:
        if time_elapsed < 60 or time_elapsed > 180:
            return

    # Already traded this slug? Use a set to track ALL traded slugs
    if not hasattr(_run_mm_cycle, '_traded_slugs'):
        _run_mm_cycle._traded_slugs = set()
    if slug in _run_mm_cycle._traded_slugs:
        return

    # ── Check conditions ──────────────────────────────
    cond = check_mm_conditions()
    if not cond.should_enter:
        return

    # ── Check mid prices — both should be near 50¢ ───
    mid_yes = fetch_midprice(live["token_yes"])
    mid_no = fetch_midprice(live["token_no"])

    if not mid_yes or not mid_no:
        return

    # Both mids should be 45-55¢ (balanced market)
    if not (0.44 <= mid_yes <= 0.56 and 0.44 <= mid_no <= 0.56):
        return

    # ── Mark slug as traded BEFORE placing orders ─────
    _run_mm_cycle._traded_slugs.add(slug)
    # Clean old slugs (keep last 20)
    if len(_run_mm_cycle._traded_slugs) > 20:
        _run_mm_cycle._traded_slugs = set(list(_run_mm_cycle._traded_slugs)[-10:])

    # ── ENTER! Buy both sides ─────────────────────────
    buy_price = _mm_config["buy_price"]
    size = _mm_config["size_usdc"]
    cid = live["condition_id"]

    # Calculate shares: minimum 5 for neg_risk markets
    import math
    raw_shares = math.ceil(size * 1.05 / buy_price * 100) / 100
    shares = max(raw_shares, 5.0)  # Polymarket min = 5 shares
    actual_cost = round(shares * buy_price, 2)

    # Buy YES
    res_yes = place_limit_buy(live["token_yes"], buy_price, actual_cost, cid)
    if not res_yes or not res_yes.get("order_id"):
        await _mm_notify(bot,
            f"⚠️ <b>MM FAIL</b> | YES buy failed\n"
            f"📌 {live['question'][:50]}")
        return

    # Check if YES filled instantly (matched on placement)
    yes_status = "live"
    yes_resp = res_yes.get("response", {})
    if yes_resp.get("status") == "matched":
        yes_status = "matched"

    await asyncio.sleep(0.5)

    # Buy NO
    res_no = place_limit_buy(live["token_no"], buy_price, actual_cost, cid)
    if not res_no or not res_no.get("order_id"):
        # Cancel/sell YES
        if yes_status == "matched":
            place_market_sell(live["token_yes"], shares, cid)
        else:
            cancel_order(res_yes["order_id"])
        await _mm_notify(bot,
            f"⚠️ <b>MM FAIL</b> | NO buy failed, reversed YES\n"
            f"📌 {live['question'][:50]}")
        return

    no_status = "live"
    no_resp = res_no.get("response", {})
    if no_resp.get("status") == "matched":
        no_status = "matched"

    _active_session = MMSession(
        slug=slug,
        condition_id=cid,
        title=live["question"],
        market_end_ts=end_ts,
        yes_token=live["token_yes"],
        yes_buy_order=res_yes["order_id"],
        yes_buy_status=yes_status,
        yes_shares=shares,
        no_token=live["token_no"],
        no_buy_order=res_no["order_id"],
        no_buy_status=no_status,
        no_shares=shares,
        phase="buying",
        entered_at=now,
        reason=cond.reason,
    )

    # If both already matched, skip to selling phase
    if yes_status == "matched" and no_status == "matched":
        _active_session.phase = "selling"

    await _mm_notify(bot,
        f"🔄 <b>MM ENTER</b> | {live['question'][:50]}\n"
        f"💰 YES 50¢ + NO 50¢ ({shares} shares each)\n"
        f"📊 {cond.reason}\n"
        f"🎯 Sell: 60¢ | SL: 40¢\n"
        f"⏱ {time_left}s left\n"
        f"YES: {yes_status} | NO: {no_status}"
    )
    _save_mm_state()


async def _manage_session(bot):
    """Manage active MM session through its phases."""
    global _active_session
    from trading import (place_limit_sell, place_market_sell,
                         check_order_status, cancel_order)
    from sniper import fetch_midprice

    s = _active_session
    if not s:
        return

    now = int(time.time())
    time_left = s.market_end_ts - now

    # ── PHASE: BUYING — wait for buy fills ────────────
    if s.phase == "buying":
        # Check YES buy
        if s.yes_buy_status == "live":
            st = check_order_status(s.yes_buy_order)
            if st and st.lower() == "matched":
                s.yes_buy_status = "matched"

        # Check NO buy
        if s.no_buy_status == "live":
            st = check_order_status(s.no_buy_order)
            if st and st.lower() == "matched":
                s.no_buy_status = "matched"

        # Both filled → move to selling
        if s.yes_buy_status == "matched" and s.no_buy_status == "matched":
            s.phase = "selling"
            logger.info("MM: Both buys filled, placing sells")
            return

        # Timeout: if buys not filled within 30s, cancel and abort
        if now - s.entered_at > 30:
            if s.yes_buy_status == "live":
                cancel_order(s.yes_buy_order)
            if s.no_buy_status == "live":
                cancel_order(s.no_buy_order)
            # If only one filled, sell it at market
            if s.yes_buy_status == "matched" and s.no_buy_status != "matched":
                place_market_sell(s.yes_token, s.yes_shares, s.condition_id)
            elif s.no_buy_status == "matched" and s.yes_buy_status != "matched":
                place_market_sell(s.no_token, s.no_shares, s.condition_id)
            await _mm_notify(bot,
                f"⚠️ <b>MM ABORT</b> | Buys not filled in 30s\n"
                f"YES: {s.yes_buy_status} | NO: {s.no_buy_status}"
            )
            _active_session = None
            return
        return

    # ── PHASE: SELLING — place sell limits ────────────
    if s.phase == "selling":
        sell_price = _mm_config["sell_target"]

        # Place YES sell
        if not s.yes_sell_order and not s.yes_closed:
            res = place_limit_sell(s.yes_token, sell_price, s.yes_shares, s.condition_id)
            if res and res.get("order_id"):
                s.yes_sell_order = res["order_id"]
                s.yes_sell_status = "live"
                logger.info("MM: YES sell placed @ %.0f¢", sell_price * 100)
            else:
                logger.error("MM: YES sell FAILED")
                await _mm_notify(bot,
                    f"⚠️ <b>MM SELL FAIL</b> | YES sell failed\n"
                    f"📌 {s.title[:40]}")

        await asyncio.sleep(0.5)

        # Place NO sell
        if not s.no_sell_order and not s.no_closed:
            res = place_limit_sell(s.no_token, sell_price, s.no_shares, s.condition_id)
            if res and res.get("order_id"):
                s.no_sell_order = res["order_id"]
                s.no_sell_status = "live"
                logger.info("MM: NO sell placed @ %.0f¢", sell_price * 100)
            else:
                logger.error("MM: NO sell FAILED")
                await _mm_notify(bot,
                    f"⚠️ <b>MM SELL FAIL</b> | NO sell failed\n"
                    f"📌 {s.title[:40]}")

        s.phase = "monitoring"
        _save_mm_state()

        sells_ok = bool(s.yes_sell_order) + bool(s.no_sell_order)
        await _mm_notify(bot,
            f"📊 <b>MM SELLING</b> | {s.title[:40]}\n"
            f"🎯 YES sell: {'✅' if s.yes_sell_order else '❌'} @ {sell_price*100:.0f}¢\n"
            f"🎯 NO sell: {'✅' if s.no_sell_order else '❌'} @ {sell_price*100:.0f}¢\n"
            f"⏱ Моніторю fills + stop loss...")
        return

    # ── PHASE: MONITORING — check sells + stop loss ───
    if s.phase == "monitoring":

        # Check YES sell fill
        if s.yes_sell_order and s.yes_sell_status == "live":
            st = check_order_status(s.yes_sell_order)
            if st and st.lower() == "matched":
                s.yes_sell_status = "matched"
                s.yes_closed = True
                s.yes_pnl = round(s.yes_shares * _mm_config["sell_target"]
                                  - s.yes_shares * _mm_config["buy_price"], 4)
                await _mm_notify(bot,
                    f"✅ <b>MM YES SOLD</b> @ 60¢ → +${s.yes_pnl:.2f}\n"
                    f"📌 {s.title[:40]}\n"
                    f"🛡 NO → stop loss at 40¢")

        # Check NO sell fill
        if s.no_sell_order and s.no_sell_status == "live":
            st = check_order_status(s.no_sell_order)
            if st and st.lower() == "matched":
                s.no_sell_status = "matched"
                s.no_closed = True
                s.no_pnl = round(s.no_shares * _mm_config["sell_target"]
                                 - s.no_shares * _mm_config["buy_price"], 4)
                await _mm_notify(bot,
                    f"✅ <b>MM NO SOLD</b> @ 60¢ → +${s.no_pnl:.2f}\n"
                    f"📌 {s.title[:40]}\n"
                    f"🛡 YES → stop loss at 40¢")

        # ── STOP LOSS CHECK ──────────────────────────
        # If one side sold, check mid of other side for stop loss
        if s.yes_closed and not s.no_closed:
            mid = fetch_midprice(s.no_token)
            if mid and mid <= _mm_config["stop_loss"]:
                if s.no_sell_order and s.no_sell_status == "live":
                    cancel_order(s.no_sell_order)
                place_market_sell(s.no_token, s.no_shares, s.condition_id)
                s.no_closed = True
                s.no_pnl = round(s.no_shares * mid
                                 - s.no_shares * _mm_config["buy_price"], 4)
                await _mm_notify(bot,
                    f"🛑 <b>MM STOP LOSS</b> | NO @ {mid*100:.0f}¢\n"
                    f"📌 {s.title[:40]}\n"
                    f"💰 NO P&L: ${s.no_pnl:.2f}")

        if s.no_closed and not s.yes_closed:
            mid = fetch_midprice(s.yes_token)
            if mid and mid <= _mm_config["stop_loss"]:
                if s.yes_sell_order and s.yes_sell_status == "live":
                    cancel_order(s.yes_sell_order)
                place_market_sell(s.yes_token, s.yes_shares, s.condition_id)
                s.yes_closed = True
                s.yes_pnl = round(s.yes_shares * mid
                                  - s.yes_shares * _mm_config["buy_price"], 4)
                await _mm_notify(bot,
                    f"🛑 <b>MM STOP LOSS</b> | YES @ {mid*100:.0f}¢\n"
                    f"📌 {s.title[:40]}\n"
                    f"💰 YES P&L: ${s.yes_pnl:.2f}")

        # ── EMERGENCY: Close everything 60s before end ─
        if time_left <= 60:
            if not s.yes_closed:
                if s.yes_sell_order and s.yes_sell_status == "live":
                    cancel_order(s.yes_sell_order)
                place_market_sell(s.yes_token, s.yes_shares, s.condition_id)
                mid = fetch_midprice(s.yes_token) or 0.40
                s.yes_closed = True
                s.yes_pnl = round(s.yes_shares * mid
                                  - s.yes_shares * _mm_config["buy_price"], 4)

            if not s.no_closed:
                if s.no_sell_order and s.no_sell_status == "live":
                    cancel_order(s.no_sell_order)
                place_market_sell(s.no_token, s.no_shares, s.condition_id)
                mid = fetch_midprice(s.no_token) or 0.40
                s.no_closed = True
                s.no_pnl = round(s.no_shares * mid
                                 - s.no_shares * _mm_config["buy_price"], 4)

        # ── Both sides closed → done ─────────────────
        if s.yes_closed and s.no_closed:
            s.phase = "done"
            total_pnl = s.yes_pnl + s.no_pnl

            _mm_stats["total_trades"] += 1
            _mm_stats["total_pnl"] += total_pnl
            if total_pnl > 0.01:
                _mm_stats["wins"] += 1
            elif total_pnl < -0.01:
                _mm_stats["losses"] += 1
            else:
                _mm_stats["breakeven"] += 1
            _save_mm_state()

            emoji = "🟩" if total_pnl > 0 else ("🟥" if total_pnl < 0 else "⬜")
            sign = "+" if total_pnl >= 0 else ""
            w = _mm_stats["wins"]
            l = _mm_stats["losses"]
            b = _mm_stats["breakeven"]
            total = w + l + b
            total_sign = "+" if _mm_stats["total_pnl"] >= 0 else ""

            await _mm_notify(bot,
                f"{emoji} <b>MM DONE</b> | {s.title[:40]}\n"
                f"  YES: {'+' if s.yes_pnl>=0 else ''}{s.yes_pnl:.2f}\n"
                f"  NO:  {'+' if s.no_pnl>=0 else ''}{s.no_pnl:.2f}\n"
                f"💰 Net: {sign}${total_pnl:.2f}\n"
                f"📈 {w}W/{l}L/{b}B | Total: {total_sign}${_mm_stats['total_pnl']:.2f}"
            )

            # Log to sheets
            _log_mm_trade(s, total_pnl)
            _active_session = None


# ── Google Sheets ────────────────────────────────────────

def _log_mm_trade(s: MMSession, total_pnl: float):
    import threading
    def _write():
        try:
            from sheets import _get_client, _get_or_create_sheet
            from datetime import datetime, timezone

            gc, spreadsheet = _get_client()
            if not gc or not spreadsheet:
                return

            ws = _get_or_create_sheet(spreadsheet, "🔄 MM")

            try:
                first_cell = ws.acell("A1").value
            except Exception:
                first_cell = None

            if not first_cell:
                headers = [
                    "Timestamp", "Market", "YES P&L", "NO P&L",
                    "Net P&L", "Result", "Reason",
                ]
                ws.update("A1:G1", [headers], value_input_option="USER_ENTERED")

                summary = [
                    ["MM STATS", ""],
                    ["Total trades", '=COUNTA(A2:A)'],
                    ["Wins", '=COUNTIF(F2:F,"WIN")'],
                    ["Losses", '=COUNTIF(F2:F,"LOSS")'],
                    ["Breakeven", '=COUNTIF(F2:F,"BE")'],
                    ["Total P&L", '=SUM(E2:E)'],
                    ["Win Rate %", '=IF(I3>0,I4/(I4+I5)*100,0)'],
                    ["Avg Net P&L", '=IFERROR(AVERAGE(E2:E),0)'],
                ]
                ws.update("I1:J8", summary, value_input_option="USER_ENTERED")

                try:
                    ws.format("A1:G1", {"textFormat": {"bold": True}})
                    ws.format("I1:I1", {"textFormat": {"bold": True}})
                except Exception:
                    pass

            result = "WIN" if total_pnl > 0.01 else ("LOSS" if total_pnl < -0.01 else "BE")
            row = [
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                s.title[:50],
                round(s.yes_pnl, 4),
                round(s.no_pnl, 4),
                round(total_pnl, 4),
                result,
                s.reason,
            ]
            ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.error("MM sheets error: %s", e)

    threading.Thread(target=_write, daemon=True).start()


# ── Persistence ──────────────────────────────────────────

def _save_mm_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS mm_bot (
            key TEXT PRIMARY KEY, value TEXT)""")
        state = {
            "active": _mm_active,
            "wins": _mm_stats["wins"],
            "losses": _mm_stats["losses"],
            "breakeven": _mm_stats["breakeven"],
            "total_pnl": round(_mm_stats["total_pnl"], 4),
            "total_trades": _mm_stats["total_trades"],
        }
        c.execute("INSERT OR REPLACE INTO mm_bot VALUES (?, ?)",
                  ("state", json.dumps(state)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Save MM state: %s", e)


def _load_mm_state() -> bool:
    from config import DB_PATH
    global _mm_active
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS mm_bot (
            key TEXT PRIMARY KEY, value TEXT)""")
        row = c.execute("SELECT value FROM mm_bot WHERE key='state'").fetchone()
        conn.close()
        if row:
            state = json.loads(row[0])
            _mm_active = state.get("active", False)
            _mm_stats["wins"] = state.get("wins", 0)
            _mm_stats["losses"] = state.get("losses", 0)
            _mm_stats["breakeven"] = state.get("breakeven", 0)
            _mm_stats["total_pnl"] = state.get("total_pnl", 0)
            _mm_stats["total_trades"] = state.get("total_trades", 0)
            _mm_stats["started_at"] = int(time.time())
            return _mm_active
    except Exception as e:
        logger.error("Load MM state: %s", e)
    return False


# ── Control ──────────────────────────────────────────────

def start_mm():
    global _mm_active
    _mm_active = True
    _mm_stats["started_at"] = int(time.time())
    _save_mm_state()


def stop_mm():
    global _mm_active, _active_session
    _mm_active = False
    # Cancel any open orders
    if _active_session:
        from trading import cancel_order, place_market_sell
        s = _active_session
        for oid, st in [(s.yes_buy_order, s.yes_buy_status),
                         (s.no_buy_order, s.no_buy_status),
                         (s.yes_sell_order, s.yes_sell_status),
                         (s.no_sell_order, s.no_sell_status)]:
            if oid and st == "live":
                try:
                    cancel_order(oid)
                except Exception:
                    pass
        _active_session = None
    _save_mm_state()


def is_mm_active() -> bool:
    return _mm_active


def get_mm_status() -> str:
    if not _mm_active:
        return "🔄 MM Bot: OFF"

    w = _mm_stats["wins"]
    l = _mm_stats["losses"]
    b = _mm_stats["breakeven"]
    total = w + l + b
    sign = "+" if _mm_stats["total_pnl"] >= 0 else ""
    runtime = int(time.time()) - _mm_stats.get("started_at", int(time.time()))
    hours = runtime // 3600

    session_text = ""
    if _active_session:
        s = _active_session
        tl = s.market_end_ts - int(time.time())
        session_text = (
            f"\n\n🔥 <b>Active:</b>\n"
            f"  📌 {s.title[:40]}\n"
            f"  Phase: {s.phase}\n"
            f"  YES: buy={s.yes_buy_status} sell={s.yes_sell_status}\n"
            f"  NO:  buy={s.no_buy_status} sell={s.no_sell_status}\n"
            f"  ⏱ {tl}s left"
        )

    return (
        f"🔄 <b>MM Bot 🟢 ON</b>\n\n"
        f"📈 {total}T | {w}W/{l}L/{b}B\n"
        f"💰 P&L: {sign}${_mm_stats['total_pnl']:.2f}\n"
        f"⚙️ Buy: 50¢ | Sell: 60¢ | SL: 40¢\n"
        f"⏱ {hours}h"
        + session_text
    )
