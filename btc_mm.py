"""
btc_mm.py — BTC Scalper (Single Side)

Strategy:
  1. Monitor 15m BTC market every 2s
  2. Wait for SHORT-TERM momentum signal:
     - BTC makes a quick move in one direction (>$30 in 2 min)
     - Mid price of that side is still cheap (< 58¢)
  3. Buy ONE side (Up or Down) based on momentum
  4. Place sell limit at entry + 10¢
  5. Stop loss at entry - 10¢
  6. Emergency close 60s before market end

Command: /mm_bot [start|stop|status]
"""
import asyncio
import json
import logging
import time
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_mm_active = False
_mm_stats = {
    "wins": 0, "losses": 0, "breakeven": 0,
    "total_pnl": 0.0, "total_trades": 0, "started_at": 0,
}


@dataclass
class ScalpTrade:
    slug: str = ""
    condition_id: str = ""
    title: str = ""
    market_end_ts: int = 0
    token_id: str = ""
    direction: str = ""
    buy_order_id: str = ""
    buy_status: str = ""
    buy_price: float = 0
    shares: float = 0
    cost: float = 0
    sell_order_id: str = ""
    sell_status: str = ""
    target_price: float = 0
    stop_price: float = 0
    closed: bool = False
    exit_price: float = 0
    pnl: float = 0
    entered_at: int = 0
    reason: str = ""
    btc_momentum: float = 0


_active_trade: ScalpTrade | None = None
_traded_slugs: set = set()


# ── Binance ──────────────────────────────────────────────

def _get_btc_price():
    import requests
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                         params={"symbol": "BTCUSDT"}, timeout=5)
        if r.status_code == 200:
            return float(r.json()["price"])
    except Exception:
        pass
    return None


def _get_1m_candles(limit=5):
    import requests
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
                         timeout=5)
        if r.status_code == 200:
            return [{"open": float(k[1]), "high": float(k[2]),
                     "low": float(k[3]), "close": float(k[4]),
                     "volume": float(k[5])} for k in r.json()]
    except Exception:
        pass
    return None


# ── Signal Detection ─────────────────────────────────────

@dataclass
class ScalpSignal:
    should_enter: bool = False
    direction: str = ""
    strength: float = 0
    reason: str = ""


def detect_scalp_signal():
    sig = ScalpSignal()
    candles = _get_1m_candles(5)
    if not candles or len(candles) < 3:
        sig.reason = "Not enough candles"
        return sig

    last2_move = candles[-1]["close"] - candles[-3]["open"]
    last_dir = candles[-1]["close"] - candles[-1]["open"]
    ranges = [c["high"] - c["low"] for c in candles[:-1]]
    avg_range = sum(ranges) / len(ranges) if ranges else 20
    min_move = max(30, avg_range * 1.5)

    if abs(last2_move) >= min_move:
        if last2_move > 0 and last_dir > 0:
            sig.should_enter = True
            sig.direction = "Up"
            sig.strength = last2_move
            sig.reason = f"BTC +${last2_move:.0f} in 2min (need ±${min_move:.0f})"
        elif last2_move < 0 and last_dir < 0:
            sig.should_enter = True
            sig.direction = "Down"
            sig.strength = abs(last2_move)
            sig.reason = f"BTC -${abs(last2_move):.0f} in 2min (need ±${min_move:.0f})"

    if not sig.should_enter:
        sig.reason = f"No momentum (${last2_move:+.0f}, need ±${min_move:.0f})"
    return sig


# ── Notify ───────────────────────────────────────────────

async def _mm_notify(bot, text):
    from config import OWNER_ID, CHANNEL_ID
    for cid in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
        except Exception:
            pass


# ── Main Loop ────────────────────────────────────────────

async def mm_checker(bot):
    logger.info("Scalp bot started (2s)")
    await asyncio.sleep(8)
    if _load_mm_state():
        await _mm_notify(bot, "🔄 <b>Scalp Bot auto-restored</b>")
    while True:
        try:
            if _mm_active:
                await _run_scalp_cycle(bot)
        except Exception as e:
            logger.error("Scalp error: %s", e)
        await asyncio.sleep(2)


async def _run_scalp_cycle(bot):
    global _active_trade
    from trading import place_limit_buy, cancel_order
    from sniper import find_live_market, fetch_midprice

    now = int(time.time())

    if _active_trade:
        await _manage_trade(bot)
        return

    live = find_live_market("15m")
    if not live:
        return

    slug = live["slug"]
    end_ts = live["end_ts"]
    time_left = end_ts - now
    time_elapsed = 900 - time_left

    if time_elapsed < 120 or time_elapsed > 600:
        return
    if time_left < 180:
        return
    if slug in _traded_slugs:
        return

    sig = detect_scalp_signal()
    if not sig.should_enter:
        return

    token_id = live["token_yes"] if sig.direction == "Up" else live["token_no"]
    mid = fetch_midprice(token_id)
    if not mid or mid < 0.40 or mid > 0.58:
        return

    _traded_slugs.add(slug)
    if len(_traded_slugs) > 30:
        _traded_slugs.clear()

    buy_price = round(min(mid + 0.02, 0.58), 2)
    target = round(buy_price + 0.10, 2)
    stop = round(buy_price - 0.10, 2)
    size_usdc = 2.50

    import math
    shares = max(math.ceil(size_usdc * 1.05 / buy_price * 100) / 100, 5.0)

    result = place_limit_buy(token_id, buy_price, size_usdc, live["condition_id"])
    if not result or not result.get("order_id"):
        await _mm_notify(bot, f"⚠️ <b>SCALP FAIL</b> | Buy failed\n📌 {live['question'][:50]}")
        return

    buy_st = "matched" if result.get("response", {}).get("status") == "matched" else "live"

    _active_trade = ScalpTrade(
        slug=slug, condition_id=live["condition_id"], title=live["question"],
        market_end_ts=end_ts, token_id=token_id, direction=sig.direction,
        buy_order_id=result["order_id"], buy_status=buy_st, buy_price=buy_price,
        shares=shares, cost=round(shares * buy_price, 2),
        target_price=target, stop_price=stop,
        entered_at=now, reason=sig.reason, btc_momentum=sig.strength,
    )

    de = "🟢" if sig.direction == "Up" else "🔴"
    await _mm_notify(bot,
        f"⚡ <b>SCALP</b> | {live['question'][:45]}\n"
        f"{de} {sig.direction} @ {buy_price*100:.0f}¢ (mid {mid*100:.0f}¢)\n"
        f"🎯 {target*100:.0f}¢ | 🛑 {stop*100:.0f}¢\n"
        f"📊 {sig.reason}\n⏱ {time_left}s | {buy_st}")
    _save_mm_state()


async def _manage_trade(bot):
    global _active_trade
    from trading import place_limit_sell, place_market_sell, check_order_status, cancel_order
    from sniper import fetch_midprice

    t = _active_trade
    if not t:
        return
    now = int(time.time())
    time_left = t.market_end_ts - now

    # Wait for buy fill
    if t.buy_status == "live":
        st = check_order_status(t.buy_order_id)
        if st and st.lower() == "matched":
            t.buy_status = "matched"
        elif now - t.entered_at > 20:
            cancel_order(t.buy_order_id)
            await _mm_notify(bot, f"⏰ <b>SCALP CANCEL</b> | Not filled 20s\n📌 {t.title[:40]}")
            _active_trade = None
            return
        else:
            return

    # Place sell
    if not t.sell_order_id and not t.closed:
        res = place_limit_sell(t.token_id, t.target_price, t.shares, t.condition_id)
        if res and res.get("order_id"):
            t.sell_order_id = res["order_id"]
            t.sell_status = "live"
        else:
            place_market_sell(t.token_id, t.shares, t.condition_id)
            mid = fetch_midprice(t.token_id) or t.buy_price
            t.exit_price = mid
            t.pnl = round(t.shares * mid - t.cost, 4)
            t.closed = True
            await _mm_notify(bot, f"⚠️ <b>SELL FAIL</b> → market sell @ {mid*100:.0f}¢\n💰 ${t.pnl:.2f}")
            await _close_trade(bot, t)
            return
        _save_mm_state()
        return

    # Check sell fill
    if t.sell_order_id and t.sell_status == "live":
        st = check_order_status(t.sell_order_id)
        if st and st.lower() == "matched":
            t.exit_price = t.target_price
            t.pnl = round(t.shares * t.target_price - t.cost, 4)
            t.closed = True
            await _close_trade(bot, t)
            return

    # Stop loss
    if not t.closed:
        mid = fetch_midprice(t.token_id)
        if mid and mid <= t.stop_price:
            if t.sell_order_id and t.sell_status == "live":
                cancel_order(t.sell_order_id)
            place_market_sell(t.token_id, t.shares, t.condition_id)
            t.exit_price = mid
            t.pnl = round(t.shares * mid - t.cost, 4)
            t.closed = True
            await _mm_notify(bot,
                f"🛑 <b>STOP LOSS</b> @ {mid*100:.0f}¢\n📌 {t.title[:40]}\n💰 ${t.pnl:.2f}")
            await _close_trade(bot, t)
            return

    # Emergency close
    if time_left <= 60 and not t.closed:
        if t.sell_order_id and t.sell_status == "live":
            cancel_order(t.sell_order_id)
        place_market_sell(t.token_id, t.shares, t.condition_id)
        mid = fetch_midprice(t.token_id) or t.buy_price
        t.exit_price = mid
        t.pnl = round(t.shares * mid - t.cost, 4)
        t.closed = True
        await _mm_notify(bot,
            f"⏰ <b>EMERGENCY CLOSE</b> @ {mid*100:.0f}¢\n📌 {t.title[:40]}\n💰 ${t.pnl:.2f}")
        await _close_trade(bot, t)


async def _close_trade(bot, t):
    global _active_trade
    _mm_stats["total_trades"] += 1
    if t.pnl > 0.01:
        _mm_stats["wins"] += 1
        result = "WIN"
    elif t.pnl < -0.01:
        _mm_stats["losses"] += 1
        result = "LOSS"
    else:
        _mm_stats["breakeven"] += 1
        result = "BE"
    _mm_stats["total_pnl"] += t.pnl
    _save_mm_state()

    w, l, b = _mm_stats["wins"], _mm_stats["losses"], _mm_stats["breakeven"]
    total = w + l + b
    wr = (w / total * 100) if total > 0 else 0
    ts = "+" if _mm_stats["total_pnl"] >= 0 else ""
    em = "🟩" if t.pnl > 0 else ("🟥" if t.pnl < 0 else "⬜")
    s = "+" if t.pnl >= 0 else ""
    de = "🟢" if t.direction == "Up" else "🔴"

    await _mm_notify(bot,
        f"{em} <b>SCALP {result}</b> | {t.title[:40]}\n"
        f"{de} {t.direction} | {t.buy_price*100:.0f}¢ → {t.exit_price*100:.0f}¢\n"
        f"💰 {s}${t.pnl:.2f} ({t.shares} shares)\n"
        f"📈 {w}W/{l}L/{b}B ({wr:.0f}%) | Total: {ts}${_mm_stats['total_pnl']:.2f}")

    _log_mm_trade(t, result)
    _active_trade = None


# ── Sheets ───────────────────────────────────────────────

def _log_mm_trade(t, result):
    import threading
    def _write():
        try:
            from sheets import _get_client, _get_or_create_sheet
            from datetime import datetime, timezone
            gc, spreadsheet = _get_client()
            if not gc or not spreadsheet:
                return
            ws = _get_or_create_sheet(spreadsheet, "⚡ Scalp")
            try:
                first = ws.acell("A1").value
            except Exception:
                first = None
            if not first:
                ws.update("A1:L1", [["Timestamp", "Market", "Direction", "Buy(¢)",
                    "Exit(¢)", "Target(¢)", "Stop(¢)", "Shares", "P&L($)",
                    "Result", "BTC Move($)", "Reason"]], value_input_option="USER_ENTERED")
                ws.update("N1:O8", [
                    ["SCALP STATS", ""], ["Total", '=COUNTA(A2:A)'],
                    ["Wins", '=COUNTIF(J2:J,"WIN")'], ["Losses", '=COUNTIF(J2:J,"LOSS")'],
                    ["WR%", '=IF(N3>0,N4/(N4+N5)*100,0)'], ["P&L", '=SUM(I2:I)'],
                    ["Avg Win", '=IFERROR(AVERAGEIF(J2:J,"WIN",I2:I),0)'],
                    ["Avg Loss", '=IFERROR(AVERAGEIF(J2:J,"LOSS",I2:I),0)'],
                ], value_input_option="USER_ENTERED")
                try:
                    ws.format("A1:L1", {"textFormat": {"bold": True}})
                except Exception:
                    pass
            ws.append_row([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                t.title[:50], t.direction, round(t.buy_price*100,1),
                round(t.exit_price*100,1), round(t.target_price*100,1),
                round(t.stop_price*100,1), t.shares, round(t.pnl,4),
                result, round(t.btc_momentum,0), t.reason,
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            logger.error("Scalp sheets: %s", e)
    threading.Thread(target=_write, daemon=True).start()


# ── Persistence ──────────────────────────────────────────

def _save_mm_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS mm_bot (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO mm_bot VALUES (?,?)", ("state", json.dumps({
            "active": _mm_active, "wins": _mm_stats["wins"], "losses": _mm_stats["losses"],
            "breakeven": _mm_stats["breakeven"], "total_pnl": round(_mm_stats["total_pnl"],4),
            "total_trades": _mm_stats["total_trades"],
        })))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Save scalp: %s", e)


def _load_mm_state():
    from config import DB_PATH
    global _mm_active
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS mm_bot (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM mm_bot WHERE key='state'").fetchone()
        conn.close()
        if row:
            s = json.loads(row[0])
            _mm_active = s.get("active", False)
            _mm_stats.update({k: s.get(k, 0) for k in ["wins","losses","breakeven","total_pnl","total_trades"]})
            _mm_stats["started_at"] = int(time.time())
            return _mm_active
    except Exception as e:
        logger.error("Load scalp: %s", e)
    return False


# ── Control ──────────────────────────────────────────────

def start_mm():
    global _mm_active
    _mm_active = True
    _mm_stats["started_at"] = int(time.time())
    _save_mm_state()

def stop_mm():
    global _mm_active, _active_trade
    _mm_active = False
    if _active_trade and not _active_trade.closed:
        from trading import cancel_order, place_market_sell
        t = _active_trade
        if t.buy_status == "live":
            cancel_order(t.buy_order_id)
        elif t.sell_order_id and t.sell_status == "live":
            cancel_order(t.sell_order_id)
            place_market_sell(t.token_id, t.shares, t.condition_id)
        _active_trade = None
    _save_mm_state()

def is_mm_active():
    return _mm_active

def get_mm_status():
    if not _mm_active:
        return "⚡ Scalp Bot: OFF"
    w, l, b = _mm_stats["wins"], _mm_stats["losses"], _mm_stats["breakeven"]
    total = w + l + b
    wr = (w / total * 100) if total > 0 else 0
    sign = "+" if _mm_stats["total_pnl"] >= 0 else ""
    hours = (int(time.time()) - _mm_stats.get("started_at", int(time.time()))) // 3600
    tt = ""
    if _active_trade:
        t = _active_trade
        tl = t.market_end_ts - int(time.time())
        de = "🟢" if t.direction == "Up" else "🔴"
        tt = (f"\n\n🔥 <b>Active:</b>\n  {de} {t.direction} @ {t.buy_price*100:.0f}¢\n"
              f"  🎯 {t.target_price*100:.0f}¢ | 🛑 {t.stop_price*100:.0f}¢\n"
              f"  📌 {t.title[:40]}\n  Buy: {t.buy_status} | Sell: {t.sell_status or '-'}\n  ⏱ {tl}s")
    return (f"⚡ <b>Scalp Bot 🟢 ON</b>\n\n📈 {total}T | {w}W/{l}L/{b}B ({wr:.0f}%)\n"
            f"💰 P&L: {sign}${_mm_stats['total_pnl']:.2f}\n⚙️ +10¢ / -10¢\n⏱ {hours}h" + tt)
