"""
btc_liquidity.py — Liquidity Scalper for BTC 15m Markets

Strategy:
  1. Fetch FULL orderbook from Polymarket CLOB API
  2. Find liquidity walls (big bid/ask clusters)
  3. Place limit BUY just above big bid wall (support)
  4. Place limit SELL near big ask wall or +8-12¢ above entry
  5. All limits → zero commission on Polymarket
  6. Stop loss: cancel sell + place limit sell at entry - 8¢

Key insight: large bid walls act as support — price bounces off them.
We buy near support, sell when price moves up. Pure orderbook trading.

Command: /liq_bot [start|stop|status]
"""
import asyncio
import json
import logging
import time
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_liq_active = False
_liq_stats = {
    "wins": 0, "losses": 0, "breakeven": 0,
    "total_pnl": 0.0, "total_trades": 0, "started_at": 0,
}


# ── Orderbook Analysis ───────────────────────────────────

@dataclass
class OrderbookAnalysis:
    """Full analysis of one side's orderbook."""
    token_id: str = ""
    side: str = ""  # "Up" or "Down"

    # Raw data
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    best_bid: float = 0
    best_ask: float = 0
    mid: float = 0
    spread: float = 0

    # Liquidity analysis
    total_bid_liq: float = 0  # total $ on bid side
    total_ask_liq: float = 0
    biggest_bid_wall: float = 0  # price of biggest bid cluster
    biggest_bid_size: float = 0  # size of that wall
    biggest_ask_wall: float = 0
    biggest_ask_size: float = 0

    # Bid wall zones (price ranges with heavy liquidity)
    support_price: float = 0  # where the wall is
    support_size: float = 0   # how big
    resistance_price: float = 0  # nearest ask wall or target
    resistance_size: float = 0

    # Signal
    has_support: bool = False
    entry_price: float = 0  # buy limit here
    exit_price: float = 0   # sell limit here
    stop_price: float = 0   # stop loss here
    score: int = 0  # 0-100 quality score


def fetch_full_orderbook(token_id: str) -> dict | None:
    """Fetch complete orderbook from CLOB API."""
    import requests
    try:
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error("Orderbook fetch error: %s", e)
    return None


def analyze_orderbook(token_id: str, side: str) -> OrderbookAnalysis:
    """Analyze orderbook for liquidity walls and trading opportunities."""
    a = OrderbookAnalysis(token_id=token_id, side=side)

    book = fetch_full_orderbook(token_id)
    if not book:
        return a

    raw_bids = book.get("bids", [])
    raw_asks = book.get("asks", [])

    # Parse and sort
    bids = sorted(
        [{"price": float(b["price"]), "size": float(b["size"])} for b in raw_bids],
        key=lambda x: x["price"], reverse=True)  # highest first
    asks = sorted(
        [{"price": float(b["price"]), "size": float(b["size"])} for b in raw_asks],
        key=lambda x: x["price"])  # lowest first

    a.bids = bids
    a.asks = asks
    a.best_bid = bids[0]["price"] if bids else 0
    a.best_ask = asks[0]["price"] if asks else 1
    a.mid = round((a.best_bid + a.best_ask) / 2, 4) if bids and asks else 0
    a.spread = round(a.best_ask - a.best_bid, 4) if bids and asks else 0

    if not bids or not asks:
        return a

    # ── Find bid walls (support) ──────────────────────
    # Group bids into price clusters (within 2¢)
    bid_clusters = _cluster_levels(bids, tolerance=0.02)
    a.total_bid_liq = sum(b["price"] * b["size"] for b in bids)

    if bid_clusters:
        biggest = max(bid_clusters, key=lambda c: c["total_size"])
        a.biggest_bid_wall = biggest["price"]
        a.biggest_bid_size = biggest["total_size"]
        a.support_price = biggest["price"]
        a.support_size = biggest["total_size"]

    # ── Find ask walls (resistance) ───────────────────
    ask_clusters = _cluster_levels(asks, tolerance=0.02)
    a.total_ask_liq = sum(ak["price"] * ak["size"] for ak in asks)

    if ask_clusters:
        biggest = max(ask_clusters, key=lambda c: c["total_size"])
        a.biggest_ask_wall = biggest["price"]
        a.biggest_ask_size = biggest["total_size"]
        a.resistance_price = biggest["price"]
        a.resistance_size = biggest["total_size"]

    # ── Determine trading signal ──────────────────────
    # Good setup: big bid wall near current price (within 5¢ of mid)
    # We buy just above the wall, sell higher

    if a.support_size <= 0 or a.mid <= 0:
        return a

    wall_distance = a.mid - a.support_price  # how far wall is from mid

    # Wall must be close to mid (within 5¢) and substantial
    if wall_distance < 0 or wall_distance > 0.05:
        return a

    # Wall must be at least 20 shares to be meaningful
    if a.support_size < 20:
        return a

    # Mid should be in tradeable range (35-65¢)
    if a.mid < 0.35 or a.mid > 0.65:
        return a

    # ── Calculate entry/exit/stop ─────────────────────
    # Entry: 1-2¢ above the wall
    a.entry_price = round(a.support_price + 0.02, 2)

    # Exit: +8-10¢ above entry, or near resistance if closer
    natural_target = round(a.entry_price + 0.10, 2)
    if a.resistance_price > 0 and a.resistance_price < natural_target:
        # Resistance is closer — exit just below it
        a.exit_price = round(a.resistance_price - 0.01, 2)
    else:
        a.exit_price = natural_target

    # Minimum profit: at least 5¢ spread
    if a.exit_price - a.entry_price < 0.05:
        a.exit_price = round(a.entry_price + 0.05, 2)

    # Stop loss: below the wall (if wall breaks, we're wrong)
    a.stop_price = round(a.support_price - 0.03, 2)

    # ── Score the setup ───────────────────────────────
    score = 0
    # Wall size (bigger = stronger support)
    if a.support_size >= 50:
        score += 30
    elif a.support_size >= 30:
        score += 20
    elif a.support_size >= 20:
        score += 10

    # Wall proximity (closer to mid = better)
    if wall_distance <= 0.02:
        score += 25
    elif wall_distance <= 0.03:
        score += 15
    elif wall_distance <= 0.05:
        score += 5

    # Bid/ask ratio (more bids = bullish)
    if a.total_bid_liq > 0 and a.total_ask_liq > 0:
        ratio = a.total_bid_liq / a.total_ask_liq
        if ratio > 1.5:
            score += 25
        elif ratio > 1.2:
            score += 15
        elif ratio > 1.0:
            score += 5

    # Spread (tighter = more active)
    if a.spread <= 0.02:
        score += 20
    elif a.spread <= 0.04:
        score += 10

    a.score = score
    a.has_support = score >= 40  # minimum quality threshold

    return a


def _cluster_levels(levels: list[dict], tolerance: float = 0.02) -> list[dict]:
    """Group orderbook levels into price clusters."""
    if not levels:
        return []

    clusters = []
    current = {"price": levels[0]["price"], "total_size": 0, "count": 0}

    for lv in levels:
        if abs(lv["price"] - current["price"]) <= tolerance:
            current["total_size"] += lv["size"]
            current["count"] += 1
        else:
            if current["total_size"] > 0:
                clusters.append(current.copy())
            current = {"price": lv["price"], "total_size": lv["size"], "count": 1}

    if current["total_size"] > 0:
        clusters.append(current)

    return clusters


# ── Active Trade ─────────────────────────────────────────

@dataclass
class LiqTrade:
    slug: str = ""
    condition_id: str = ""
    title: str = ""
    market_end_ts: int = 0
    token_id: str = ""
    direction: str = ""

    buy_order_id: str = ""
    buy_status: str = ""  # live, matched
    buy_price: float = 0
    shares: float = 0
    cost: float = 0

    sell_order_id: str = ""
    sell_status: str = ""
    exit_price: float = 0  # target sell price

    stop_order_id: str = ""
    stop_status: str = ""
    stop_price: float = 0

    closed: bool = False
    close_price: float = 0
    pnl: float = 0

    entered_at: int = 0
    reason: str = ""
    wall_size: float = 0
    wall_price: float = 0
    score: int = 0


_active_trade: LiqTrade | None = None
_traded_slugs: set = set()


# ── Notifications ────────────────────────────────────────

async def _notify(bot, text):
    from config import OWNER_ID, CHANNEL_ID
    for cid in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
        except Exception:
            pass


# ── Main Loop ────────────────────────────────────────────

async def liq_checker(bot):
    """Background loop — every 3 seconds."""
    logger.info("Liquidity scalper started (3s)")
    await asyncio.sleep(10)
    if _load_state():
        await _notify(bot, "🔄 <b>Liquidity Scalper auto-restored</b>")

    while True:
        try:
            if _liq_active:
                await _run_liq_cycle(bot)
        except Exception as e:
            logger.error("Liq scalper error: %s", e)
        await asyncio.sleep(3)


async def _run_liq_cycle(bot):
    global _active_trade
    from trading import place_limit_buy, place_limit_sell, cancel_order
    from sniper import find_live_market

    now = int(time.time())

    if _active_trade:
        await _manage_liq_trade(bot)
        return

    live = find_live_market("15m")
    if not live:
        return

    slug = live["slug"]
    end_ts = live["end_ts"]
    time_left = end_ts - now
    time_elapsed = 900 - time_left

    # Enter from 30s into market until minute 10
    if time_elapsed < 30 or time_elapsed > 600:
        return
    if time_left < 120:
        return
    if slug in _traded_slugs:
        return

    # ── Analyze BOTH orderbooks ───────────────────────
    a_up = analyze_orderbook(live["token_yes"], "Up")
    a_down = analyze_orderbook(live["token_no"], "Down")

    # Pick the better setup
    best = None
    if a_up.has_support and a_down.has_support:
        best = a_up if a_up.score >= a_down.score else a_down
    elif a_up.has_support:
        best = a_up
    elif a_down.has_support:
        best = a_down
    else:
        return  # No good setup on either side

    # ── ENTER — all limits! ───────────────────────────
    _traded_slugs.add(slug)
    if len(_traded_slugs) > 30:
        _traded_slugs.clear()

    size_usdc = 1.0  # $1 per trade

    # Place limit buy
    result = place_limit_buy(
        best.token_id, best.entry_price, size_usdc, live["condition_id"])
    if not result or not result.get("order_id"):
        await _notify(bot, f"⚠️ <b>LIQ FAIL</b> | Buy failed\n📌 {live['question'][:50]}")
        return

    # Get actual shares from order (may be 5 min on neg_risk)
    shares = result.get("size", 5.0)
    cost = round(shares * best.entry_price, 2)
    buy_st = "matched" if result.get("response", {}).get("status") == "matched" else "live"

    _active_trade = LiqTrade(
        slug=slug, condition_id=live["condition_id"],
        title=live["question"], market_end_ts=end_ts,
        token_id=best.token_id, direction=best.side,
        buy_order_id=result["order_id"], buy_status=buy_st,
        buy_price=best.entry_price, shares=shares,
        cost=cost,
        exit_price=best.exit_price, stop_price=best.stop_price,
        entered_at=now,
        reason=(f"Wall {best.support_size:.0f} shares @ {best.support_price*100:.0f}¢ | "
                f"Score {best.score}"),
        wall_size=best.support_size, wall_price=best.support_price,
        score=best.score,
    )

    de = "🟢" if best.side == "Up" else "🔴"
    profit_cents = round((best.exit_price - best.entry_price) * 100)

    await _notify(bot,
        f"📊 <b>LIQ SCALP</b> | {live['question'][:45]}\n"
        f"{de} {best.side} @ {best.entry_price*100:.0f}¢\n"
        f"🧱 Wall: {best.support_size:.0f} shares @ {best.support_price*100:.0f}¢\n"
        f"🎯 Target: {best.exit_price*100:.0f}¢ (+{profit_cents}¢)\n"
        f"🛑 Stop: {best.stop_price*100:.0f}¢\n"
        f"📈 Score: {best.score}/100\n"
        f"⏱ {time_left}s | {buy_st}")
    _save_state()


async def _manage_liq_trade(bot):
    global _active_trade
    from trading import place_limit_sell, cancel_order, check_order_status
    from sniper import fetch_midprice

    t = _active_trade
    if not t:
        return
    now = int(time.time())
    time_left = t.market_end_ts - now

    # ── Wait for buy fill ─────────────────────────────
    if t.buy_status == "live":
        st = check_order_status(t.buy_order_id)
        if st and st.lower() == "matched":
            t.buy_status = "matched"
            await _notify(bot,
                f"✅ <b>LIQ FILLED</b> | {t.direction} @ {t.buy_price*100:.0f}¢\n"
                f"📌 {t.title[:40]}\n"
                f"🎯 Sell limit → {t.exit_price*100:.0f}¢")
        elif now - t.entered_at > 60:
            cancel_order(t.buy_order_id)
            await _notify(bot, f"⏰ <b>LIQ CANCEL</b> | Not filled 60s\n📌 {t.title[:40]}")
            _active_trade = None
            return
        else:
            return

    # ── Place sell limit (if not placed) ──────────────
    if not t.sell_order_id and not t.closed:
        res = place_limit_sell(t.token_id, t.exit_price, t.shares, t.condition_id)
        if res and res.get("order_id"):
            t.sell_order_id = res["order_id"]
            t.sell_status = "live"
        else:
            # Sell failed — try market sell
            from trading import place_market_sell
            place_market_sell(t.token_id, t.shares, t.condition_id)
            mid = fetch_midprice(t.token_id) or t.buy_price
            t.close_price = mid
            t.pnl = round(t.shares * mid - t.cost, 4)
            t.closed = True
            await _notify(bot, f"⚠️ <b>LIQ SELL FAIL</b> → market\n💰 ${t.pnl:.2f}")
            await _close_liq_trade(bot, t)
            return
        _save_state()
        return

    # ── Check sell fill ───────────────────────────────
    if t.sell_order_id and t.sell_status == "live":
        st = check_order_status(t.sell_order_id)
        if st and st.lower() == "matched":
            t.close_price = t.exit_price
            t.pnl = round(t.shares * t.exit_price - t.cost, 4)
            t.closed = True
            await _close_liq_trade(bot, t)
            return

    # ── Check stop loss (via mid price) ───────────────
    if not t.closed:
        mid = fetch_midprice(t.token_id)
        if mid and mid <= t.stop_price:
            # Cancel sell limit, place stop limit sell
            if t.sell_order_id and t.sell_status == "live":
                cancel_order(t.sell_order_id)

            # Try limit sell at stop price first (zero commission)
            stop_res = place_limit_sell(
                t.token_id, t.stop_price, t.shares, t.condition_id)
            if stop_res and stop_res.get("order_id"):
                t.stop_order_id = stop_res["order_id"]
                t.stop_status = "live"
                # Check if it filled instantly
                resp = stop_res.get("response", {})
                if resp.get("status") == "matched":
                    t.close_price = t.stop_price
                    t.pnl = round(t.shares * t.stop_price - t.cost, 4)
                    t.closed = True
                    await _notify(bot,
                        f"🛑 <b>LIQ STOP</b> @ {t.stop_price*100:.0f}¢ (limit)\n"
                        f"📌 {t.title[:40]}\n💰 ${t.pnl:.2f}")
                    await _close_liq_trade(bot, t)
                    return
                # Not instant — will check next cycle
                await _notify(bot,
                    f"🛑 <b>LIQ STOP PLACED</b> @ {t.stop_price*100:.0f}¢\n"
                    f"📌 {t.title[:40]}")
                t.sell_order_id = ""  # clear old sell
                t.sell_status = ""
            else:
                # Limit stop failed — market sell as fallback
                from trading import place_market_sell
                place_market_sell(t.token_id, t.shares, t.condition_id)
                t.close_price = mid
                t.pnl = round(t.shares * mid - t.cost, 4)
                t.closed = True
                await _notify(bot,
                    f"🛑 <b>LIQ STOP</b> @ {mid*100:.0f}¢ (market)\n"
                    f"📌 {t.title[:40]}\n💰 ${t.pnl:.2f}")
                await _close_liq_trade(bot, t)
            return

    # ── Check stop order fill ─────────────────────────
    if t.stop_order_id and t.stop_status == "live":
        st = check_order_status(t.stop_order_id)
        if st and st.lower() == "matched":
            t.close_price = t.stop_price
            t.pnl = round(t.shares * t.stop_price - t.cost, 4)
            t.closed = True
            await _close_liq_trade(bot, t)
            return

    # ── Emergency close 60s before end ────────────────
    if time_left <= 60 and not t.closed:
        for oid, ost in [(t.sell_order_id, t.sell_status),
                         (t.stop_order_id, t.stop_status)]:
            if oid and ost == "live":
                cancel_order(oid)
        from trading import place_market_sell
        place_market_sell(t.token_id, t.shares, t.condition_id)
        mid = fetch_midprice(t.token_id) or t.buy_price
        t.close_price = mid
        t.pnl = round(t.shares * mid - t.cost, 4)
        t.closed = True
        await _notify(bot,
            f"⏰ <b>LIQ EMERGENCY</b> @ {mid*100:.0f}¢\n📌 {t.title[:40]}\n💰 ${t.pnl:.2f}")
        await _close_liq_trade(bot, t)


async def _close_liq_trade(bot, t):
    global _active_trade
    _liq_stats["total_trades"] += 1
    if t.pnl > 0.01:
        _liq_stats["wins"] += 1
        result = "WIN"
    elif t.pnl < -0.01:
        _liq_stats["losses"] += 1
        result = "LOSS"
    else:
        _liq_stats["breakeven"] += 1
        result = "BE"
    _liq_stats["total_pnl"] += t.pnl
    _save_state()

    w, l, b = _liq_stats["wins"], _liq_stats["losses"], _liq_stats["breakeven"]
    total = w + l + b
    wr = (w / total * 100) if total > 0 else 0
    ts = "+" if _liq_stats["total_pnl"] >= 0 else ""
    em = "🟩" if t.pnl > 0 else ("🟥" if t.pnl < 0 else "⬜")
    s = "+" if t.pnl >= 0 else ""
    de = "🟢" if t.direction == "Up" else "🔴"

    await _notify(bot,
        f"{em} <b>LIQ {result}</b> | {t.title[:40]}\n"
        f"{de} {t.direction} | {t.buy_price*100:.0f}¢ → {t.close_price*100:.0f}¢\n"
        f"🧱 Wall was: {t.wall_size:.0f}sh @ {t.wall_price*100:.0f}¢\n"
        f"💰 {s}${t.pnl:.2f} ({t.shares} shares)\n"
        f"📈 {w}W/{l}L/{b}B ({wr:.0f}%) | Total: {ts}${_liq_stats['total_pnl']:.2f}")

    _log_liq_trade(t, result)
    _active_trade = None


# ── Google Sheets ────────────────────────────────────────

def _log_liq_trade(t, result):
    import threading
    def _write():
        try:
            from sheets import _get_client, _get_or_create_sheet
            from datetime import datetime, timezone
            gc, spreadsheet = _get_client()
            if not gc or not spreadsheet:
                return
            ws = _get_or_create_sheet(spreadsheet, "📊 Liquidity")
            try:
                first = ws.acell("A1").value
            except Exception:
                first = None
            if not first:
                ws.update("A1:M1", [["Timestamp", "Market", "Direction", "Buy(¢)",
                    "Exit(¢)", "Stop(¢)", "Shares", "P&L($)", "Result",
                    "Wall Size", "Wall Price(¢)", "Score", "Reason"]],
                    value_input_option="USER_ENTERED")
                ws.update("O1:P8", [
                    ["LIQ STATS", ""], ["Total", '=COUNTA(A2:A)'],
                    ["Wins", '=COUNTIF(I2:I,"WIN")'], ["Losses", '=COUNTIF(I2:I,"LOSS")'],
                    ["WR%", '=IF(O3>0,O4/(O4+O5)*100,0)'], ["P&L", '=SUM(H2:H)'],
                    ["Avg Win", '=IFERROR(AVERAGEIF(I2:I,"WIN",H2:H),0)'],
                    ["Avg Loss", '=IFERROR(AVERAGEIF(I2:I,"LOSS",H2:H),0)'],
                ], value_input_option="USER_ENTERED")
                try:
                    ws.format("A1:M1", {"textFormat": {"bold": True}})
                except Exception:
                    pass
            ws.append_row([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                t.title[:50], t.direction, round(t.buy_price*100,1),
                round(t.close_price*100,1), round(t.stop_price*100,1),
                t.shares, round(t.pnl,4), result,
                round(t.wall_size,0), round(t.wall_price*100,1),
                t.score, t.reason,
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            logger.error("Liq sheets: %s", e)
    threading.Thread(target=_write, daemon=True).start()


# ── Persistence ──────────────────────────────────────────

def _save_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS liq_bot (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO liq_bot VALUES (?,?)", ("state", json.dumps({
            "active": _liq_active, **{k: _liq_stats[k] for k in
            ["wins","losses","breakeven","total_pnl","total_trades"]},
        })))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Save liq: %s", e)


def _load_state():
    from config import DB_PATH
    global _liq_active
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS liq_bot (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM liq_bot WHERE key='state'").fetchone()
        conn.close()
        if row:
            s = json.loads(row[0])
            _liq_active = s.get("active", False)
            _liq_stats.update({k: s.get(k, 0) for k in
                ["wins","losses","breakeven","total_pnl","total_trades"]})
            _liq_stats["started_at"] = int(time.time())
            return _liq_active
    except Exception as e:
        logger.error("Load liq: %s", e)
    return False


# ── Control ──────────────────────────────────────────────

def start_liq():
    global _liq_active
    _liq_active = True
    _liq_stats["started_at"] = int(time.time())
    _save_state()

def stop_liq():
    global _liq_active, _active_trade
    _liq_active = False
    if _active_trade and not _active_trade.closed:
        from trading import cancel_order
        for oid in [_active_trade.buy_order_id, _active_trade.sell_order_id,
                    _active_trade.stop_order_id]:
            if oid:
                try:
                    cancel_order(oid)
                except Exception:
                    pass
        _active_trade = None
    _save_state()

def is_liq_active():
    return _liq_active

def get_liq_status():
    if not _liq_active:
        return "📊 Liquidity Scalper: OFF"
    w, l, b = _liq_stats["wins"], _liq_stats["losses"], _liq_stats["breakeven"]
    total = w + l + b
    wr = (w / total * 100) if total > 0 else 0
    sign = "+" if _liq_stats["total_pnl"] >= 0 else ""
    hours = (int(time.time()) - _liq_stats.get("started_at", int(time.time()))) // 3600
    tt = ""
    if _active_trade:
        t = _active_trade
        tl = t.market_end_ts - int(time.time())
        de = "🟢" if t.direction == "Up" else "🔴"
        tt = (f"\n\n🔥 <b>Active:</b>\n  {de} {t.direction} @ {t.buy_price*100:.0f}¢\n"
              f"  🧱 Wall: {t.wall_size:.0f}sh @ {t.wall_price*100:.0f}¢\n"
              f"  🎯 {t.exit_price*100:.0f}¢ | 🛑 {t.stop_price*100:.0f}¢\n"
              f"  📌 {t.title[:40]}\n  Buy: {t.buy_status} | Sell: {t.sell_status or '-'}\n  ⏱ {tl}s")
    return (f"📊 <b>Liquidity Scalper 🟢 ON</b>\n\n"
            f"📈 {total}T | {w}W/{l}L/{b}B ({wr:.0f}%)\n"
            f"💰 P&L: {sign}${_liq_stats['total_pnl']:.2f}\n"
            f"⚙️ All limits (0% commission)\n⏱ {hours}h" + tt)
