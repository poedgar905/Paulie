"""
Sniper module — late-entry directional strategy on BTC 15min/hourly markets.

Strategy "Late Sniper":
1. Bot monitors Polymarket BTC Up/Down markets
2. Waits until N minutes before market closes (e.g. 3 min for 15min, 5 min for 1h)
3. Checks Binance BTC price vs market start price (from kline open)
4. If BTC clearly trending one direction → places limit buy on winning side
5. If filled → holds until resolution
6. Stop-loss: if price drops X¢ from entry → market sell
7. Auto-rolls to next market period

Trigger logic:
- Get BTC kline open price for current period
- Get current BTC price from Binance
- If BTC change > threshold → direction is clear → enter
- Entry price: configurable (default 85¢)
- Side: auto-selected based on BTC direction (UP if rising, DOWN if falling)
"""
import asyncio
import logging
import time
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Active sessions ──────────────────────────────────────────
_sessions: dict[str, "SnipeSession"] = {}
_auto_sniper: "AutoSniper | None" = None


@dataclass
class SnipeSession:
    """One active sniper on a specific market."""
    condition_id: str
    token_id: str
    outcome: str           # "Up" or "Down"
    title: str
    event_slug: str

    # Order params
    entry_price: float     # e.g. 0.85
    size_usdc: float
    side: str              # "YES"

    # Current order
    order_id: str = ""
    order_status: str = ""  # "live", "matched", "cancelled"

    # Tracking
    fills: int = 0
    total_spent: float = 0
    total_shares: float = 0

    # State
    active: bool = True
    stop_loss_cents: int = 10
    started_at: int = 0
    last_check: int = 0
    error_count: int = 0
    market_end_ts: int = 0


@dataclass
class AutoSniper:
    """Auto-sniper config and stats."""
    active: bool = True
    market_type: str = "15m"
    entry_price: float = 0.85
    size_usdc: float = 1.0
    stop_loss_cents: int = 10
    enter_before_sec: int = 180   # 3 min before close
    min_btc_move_pct: float = 0.03  # 0.03% min BTC move

    # Stats
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0
    total_trades: int = 0

    # Current market
    current_slug: str = ""
    current_cid: str = ""
    current_entered: bool = False
    started_at: int = 0


def get_session(cid: str) -> SnipeSession | None:
    return _sessions.get(cid)

def get_all_sessions() -> list[SnipeSession]:
    return list(_sessions.values())

def get_auto_sniper() -> "AutoSniper | None":
    return _auto_sniper

def remove_session(cid: str):
    _sessions.pop(cid, None)


# ── Binance BTC Price ────────────────────────────────────────

def get_btc_price() -> float | None:
    """Current BTC/USDT from Binance. No auth needed."""
    try:
        import requests
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=5,
        )
        if resp.status_code == 200:
            return float(resp.json()["price"])
    except Exception as e:
        logger.error("Binance price error: %s", e)
    return None


def get_btc_kline(interval: str = "15m", limit: int = 1) -> dict | None:
    """BTC kline (open/high/low/close) from Binance."""
    try:
        import requests
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data:
                k = data[-1]
                return {
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": k[6],
                }
    except Exception as e:
        logger.error("Binance kline error: %s", e)
    return None


# ── Polymarket helpers ───────────────────────────────────────

def fetch_event_by_slug(slug: str) -> dict | None:
    try:
        import requests
        resp = requests.get(
            f"https://gamma-api.polymarket.com/events/slug/{slug}",
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.error("Event fetch error: %s", e)
    return None


def fetch_market_by_condition(condition_id: str) -> dict | None:
    try:
        import requests
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"condition_id": condition_id},
            timeout=10,
        )
        if resp.status_code == 200:
            markets = resp.json()
            if isinstance(markets, list) and markets:
                return markets[0]
    except Exception as e:
        logger.error("Market fetch error: %s", e)
    return None


def get_token_id(condition_id: str, outcome: str) -> str | None:
    from trading import get_token_id_for_market
    return get_token_id_for_market(condition_id, outcome)


def fetch_midprice(token_id: str) -> float | None:
    try:
        import requests
        resp = requests.get(
            "https://clob.polymarket.com/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        if resp.status_code == 200:
            return float(resp.json().get("mid", 0))
    except Exception:
        pass
    return None


def fetch_orderbook(token_id: str) -> dict | None:
    try:
        import requests
        resp = requests.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 1
            mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask < 1 else 0
            return {
                "bids": bids[:5], "asks": asks[:5],
                "best_bid": best_bid, "best_ask": best_ask,
                "mid": round(mid, 4),
                "spread": round(best_ask - best_bid, 4),
            }
    except Exception as e:
        logger.error("Orderbook error: %s", e)
    return None


def find_current_market_slug(market_type: str = "15m") -> str | None:
    """Calculate slug for current live BTC up/down market."""
    now = int(time.time())
    intervals = {"5m": (300, "btc-updown-5m-"), "15m": (900, "btc-updown-15m-"),
                 "1h": (3600, "btc-updown-1h-"), "4h": (14400, "btc-updown-4h-")}
    if market_type not in intervals:
        return None
    interval, prefix = intervals[market_type]
    period_start = (now // interval) * interval
    return f"{prefix}{period_start}"


def get_market_end_timestamp(slug: str, market_type: str = "15m") -> int:
    match = re.search(r'(\d{10})$', slug)
    if not match:
        return 0
    start_ts = int(match.group(1))
    interval = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(market_type, 900)
    return start_ts + interval


# ── Manual snipe ─────────────────────────────────────────────

def start_manual_snipe(
    condition_id, token_id, outcome, title, event_slug,
    entry_price, size_usdc, stop_loss_cents=10, market_type="15m",
) -> SnipeSession | None:
    from trading import place_limit_buy

    if condition_id in _sessions:
        return None

    result = place_limit_buy(token_id, entry_price, size_usdc, condition_id)
    if not result or not result.get("order_id"):
        return None

    session = SnipeSession(
        condition_id=condition_id, token_id=token_id,
        outcome=outcome, title=title, event_slug=event_slug,
        entry_price=entry_price, size_usdc=size_usdc, side="YES",
        order_id=result["order_id"], order_status="live",
        active=True, stop_loss_cents=stop_loss_cents,
        started_at=int(time.time()),
        market_end_ts=get_market_end_timestamp(event_slug, market_type),
    )
    _sessions[condition_id] = session
    return session


# ── Auto-sniper control ─────────────────────────────────────

def start_auto_sniper(
    market_type="15m", entry_price=0.85, size_usdc=1.0,
    stop_loss_cents=10, enter_before_sec=180, min_btc_move_pct=0.03,
) -> AutoSniper:
    global _auto_sniper
    _auto_sniper = AutoSniper(
        active=True, market_type=market_type,
        entry_price=entry_price, size_usdc=size_usdc,
        stop_loss_cents=stop_loss_cents,
        enter_before_sec=enter_before_sec,
        min_btc_move_pct=min_btc_move_pct,
        started_at=int(time.time()),
    )
    return _auto_sniper


def stop_auto_sniper() -> AutoSniper | None:
    global _auto_sniper
    sniper = _auto_sniper
    if sniper:
        sniper.active = False
        _auto_sniper = None
    return sniper


def stop_all() -> tuple[list[SnipeSession], "AutoSniper | None"]:
    from trading import cancel_order
    stopped = []
    for cid in list(_sessions.keys()):
        s = _sessions.pop(cid)
        s.active = False
        if s.order_id and s.order_status == "live":
            cancel_order(s.order_id)
        stopped.append(s)
    auto = stop_auto_sniper()
    return stopped, auto


# ── Background checker ───────────────────────────────────────

async def sniper_checker(bot):
    """Main loop — every 3 seconds."""
    from config import OWNER_ID
    logger.info("Sniper checker started (3s)")
    await asyncio.sleep(5)

    while True:
        try:
            if _auto_sniper and _auto_sniper.active:
                try:
                    await _run_auto_sniper(bot)
                except Exception as e:
                    logger.error("Auto-sniper error: %s", e)

            for cid, session in list(_sessions.items()):
                if not session.active:
                    continue
                try:
                    await _check_session(bot, session)
                except Exception as e:
                    session.error_count += 1
                    logger.error("Session error: %s", e)
                    if session.error_count >= 10:
                        session.active = False
                        remove_session(cid)
                await asyncio.sleep(0.2)

        except Exception as e:
            logger.error("Sniper loop error: %s", e)

        await asyncio.sleep(3)


async def _run_auto_sniper(bot):
    """Auto-sniper: wait for entry window, check BTC, enter."""
    from trading import place_limit_buy
    from config import OWNER_ID

    auto = _auto_sniper
    if not auto or not auto.active:
        return

    now = int(time.time())
    slug = find_current_market_slug(auto.market_type)
    if not slug:
        return

    end_ts = get_market_end_timestamp(slug, auto.market_type)
    time_left = end_ts - now

    if time_left <= 0:
        auto.current_slug = ""
        auto.current_entered = False
        return

    if slug != auto.current_slug:
        auto.current_slug = slug
        auto.current_cid = ""
        auto.current_entered = False

    if auto.current_entered:
        return

    # Not time yet?
    if time_left > auto.enter_before_sec:
        return

    # ── DECISION TIME ─────────────────────────────────────

    # BTC current price
    btc_now = get_btc_price()
    if not btc_now:
        return

    # BTC period open price
    kline_interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}.get(auto.market_type, "15m")
    kline = get_btc_kline(kline_interval, 1)
    if not kline:
        return

    btc_open = kline["open"]
    btc_change = btc_now - btc_open
    btc_change_pct = abs(btc_change / btc_open) * 100

    # Not enough move?
    if btc_change_pct < auto.min_btc_move_pct:
        return

    # Direction
    if btc_change > 0:
        direction = "Up"
        buy_outcome = "yes"
    else:
        direction = "Down"
        buy_outcome = "no"

    # Fetch Polymarket event
    event = fetch_event_by_slug(slug)
    if not event:
        return

    markets = event.get("markets", [])
    if not markets:
        return

    market = markets[0]
    cid = market.get("conditionId", "")
    title = market.get("question", event.get("title", "?"))

    token_id = get_token_id(cid, buy_outcome)
    if not token_id:
        return

    # Check if already too expensive
    mid = fetch_midprice(token_id)
    if mid and mid > auto.entry_price:
        auto.current_entered = True
        return

    # PLACE ORDER
    result = place_limit_buy(token_id, auto.entry_price, auto.size_usdc, cid)
    if not result or not result.get("order_id"):
        return

    auto.current_entered = True
    auto.current_cid = cid
    auto.total_trades += 1

    session = SnipeSession(
        condition_id=cid, token_id=token_id,
        outcome=direction, title=title, event_slug=slug,
        entry_price=auto.entry_price, size_usdc=auto.size_usdc,
        side="YES", order_id=result["order_id"], order_status="live",
        active=True, stop_loss_cents=auto.stop_loss_cents,
        started_at=now, market_end_ts=end_ts,
    )
    _sessions[cid] = session

    try:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🎯 <b>AUTO-SNIPE!</b>\n\n"
                f"📌 {title[:60]}\n"
                f"{'🟢' if direction == 'Up' else '🔴'} {direction} @ {auto.entry_price*100:.0f}¢\n"
                f"💵 ${auto.size_usdc:.2f}\n"
                f"📊 BTC: ${btc_open:,.0f} → ${btc_now:,.0f} ({'+' if btc_change > 0 else ''}{btc_change:,.0f}, {btc_change_pct:.3f}%)\n"
                f"⏱ {time_left}s left | 🛡 SL: {auto.stop_loss_cents}¢"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _check_session(bot, session: SnipeSession):
    """Check fill, stop-loss, resolution."""
    from trading import check_order_status, cancel_order, place_market_sell
    from config import OWNER_ID

    now = int(time.time())

    # ── Check fill ────────────────────────────────────────
    if session.order_id and session.order_status == "live":
        status = check_order_status(session.order_id)
        status_lower = (status or "").lower()

        if status_lower == "matched":
            session.order_status = "matched"
            shares = round(session.size_usdc / session.entry_price, 2)
            session.fills += 1
            session.total_spent += session.size_usdc
            session.total_shares += shares

            try:
                await bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"✅ <b>FILL!</b> {session.outcome} @ {session.entry_price*100:.0f}¢\n"
                        f"📌 {session.title[:50]}\n"
                        f"📊 {shares:.1f} shares = ${session.size_usdc:.2f}"
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        elif status_lower in ("cancelled", "expired"):
            session.active = False
            remove_session(session.condition_id)
            return

    # ── Stop-loss ─────────────────────────────────────────
    if session.order_status == "matched" and session.stop_loss_cents > 0:
        mid = fetch_midprice(session.token_id)
        if mid and mid > 0:
            drop = session.entry_price - mid
            if drop >= session.stop_loss_cents / 100:
                place_market_sell(session.token_id, session.total_shares, session.condition_id)
                pnl = (mid * session.total_shares) - session.total_spent

                if _auto_sniper:
                    _auto_sniper.losses += 1
                    _auto_sniper.total_pnl += pnl

                # Log to sheets
                from datetime import datetime, timezone
                log_trade_to_sheets(
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    market_title=session.title,
                    market_type=_auto_sniper.market_type if _auto_sniper else "manual",
                    direction=session.outcome,
                    entry_price=session.entry_price,
                    size_usdc=session.total_spent,
                    shares=session.total_shares,
                    result="STOP-LOSS",
                    pnl=pnl,
                )

                try:
                    await bot.send_message(
                        chat_id=OWNER_ID,
                        text=(
                            f"🛑 <b>STOP-LOSS!</b>\n"
                            f"📌 {session.title[:50]}\n"
                            f"Entry: {session.entry_price*100:.0f}¢ → {mid*100:.0f}¢\n"
                            f"💰 ${pnl:.2f}"
                            + (f"\n📈 {_auto_sniper.wins}W/{_auto_sniper.losses}L = ${_auto_sniper.total_pnl:.2f}" if _auto_sniper else "")
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                session.active = False
                remove_session(session.condition_id)
                return

    # ── Resolution ────────────────────────────────────────
    if session.order_status == "matched" and session.market_end_ts > 0:
        if now > session.market_end_ts + 30:
            market = fetch_market_by_condition(session.condition_id)
            if market and market.get("closed") and market.get("resolution"):
                resolution = market["resolution"]
                won = _check_win(session.outcome, resolution)
                shares = session.total_shares

                pnl = (shares * 1.0 - session.total_spent) if won else -session.total_spent

                if _auto_sniper:
                    if won:
                        _auto_sniper.wins += 1
                    else:
                        _auto_sniper.losses += 1
                    _auto_sniper.total_pnl += pnl

                # Log to sheets
                from datetime import datetime, timezone
                log_trade_to_sheets(
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    market_title=session.title,
                    market_type=_auto_sniper.market_type if _auto_sniper else "manual",
                    direction=session.outcome,
                    entry_price=session.entry_price,
                    size_usdc=session.total_spent,
                    shares=session.total_shares,
                    result="WIN" if won else "LOSS",
                    pnl=pnl,
                )

                emoji = "🟩" if won else "🟥"
                try:
                    await bot.send_message(
                        chat_id=OWNER_ID,
                        text=(
                            f"{emoji} <b>{'WIN' if won else 'LOSS'}!</b> {session.outcome} @ {session.entry_price*100:.0f}¢\n"
                            f"📌 {session.title[:50]}\n"
                            f"Resolved: {resolution} | 💰 {'+'if pnl>=0 else ''}${pnl:.2f}"
                            + (f"\n📈 {_auto_sniper.wins}W/{_auto_sniper.losses}L = ${_auto_sniper.total_pnl:.2f}" if _auto_sniper else "")
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                session.active = False
                remove_session(session.condition_id)

    # ── Cancel unfilled at market end ─────────────────────
    if session.order_status == "live" and session.market_end_ts > 0:
        if now > session.market_end_ts:
            cancel_order(session.order_id)
            session.active = False
            remove_session(session.condition_id)


def _check_win(outcome: str, resolution: str) -> bool:
    res = resolution.lower().strip()
    out = outcome.lower().strip()
    if out in ("up", "yes") and res in ("up", "yes", "1"):
        return True
    if out in ("down", "no") and res in ("down", "no", "0"):
        return True
    return False


# ── Google Sheets logging ────────────────────────────────────

def log_trade_to_sheets(
    timestamp: str,
    market_title: str,
    market_type: str,
    direction: str,      # "Up" or "Down"
    entry_price: float,
    size_usdc: float,
    shares: float,
    result: str,         # "WIN", "LOSS", "STOP-LOSS"
    pnl: float,
    btc_open: float = 0,
    btc_close: float = 0,
):
    """Log a completed sniper trade to Google Sheets '🎯 Sniper' tab."""
    try:
        from sheets import _get_client, _get_or_create_sheet

        gc, spreadsheet = _get_client()
        if not gc or not spreadsheet:
            return

        ws = _get_or_create_sheet(spreadsheet, "🎯 Sniper")

        # Check if headers exist
        try:
            first_cell = ws.acell("A1").value
        except Exception:
            first_cell = None

        if not first_cell:
            # Create headers + formulas
            headers = [
                "Timestamp", "Market", "Type", "Direction",
                "Entry (¢)", "Size ($)", "Shares",
                "Result", "P&L ($)",
                "BTC Open", "BTC Close", "BTC Change",
            ]
            ws.update("A1:L1", [headers])

            # Summary formulas in column N
            summary = [
                ["STATS", ""],
                ["Total trades", '=COUNTA(A2:A)'],
                ["Wins", '=COUNTIF(H2:H,"WIN")'],
                ["Losses", '=COUNTIF(H2:H,"LOSS")'],
                ["Stop-losses", '=COUNTIF(H2:H,"STOP-LOSS")'],
                ["Win Rate %", '=IF(N3>0,N4/(N4+N5+N6)*100,0)'],
                ["Total P&L $", '=SUM(I2:I)'],
                ["Total Spent $", '=SUM(F2:F)'],
                ["ROI %", '=IF(N9>0,N8/N9*100,0)'],
                ["Avg Win $", '=IF(N4>0,SUMIF(H2:H,"WIN",I2:I)/N4,0)'],
                ["Avg Loss $", '=IF((N5+N6)>0,(SUMIF(H2:H,"LOSS",I2:I)+SUMIF(H2:H,"STOP-LOSS",I2:I))/(N5+N6),0)'],
                ["Best Trade $", '=MAX(I2:I)'],
                ["Worst Trade $", '=MIN(I2:I)'],
            ]
            ws.update("N1:O13", summary)

            # Format header row bold
            try:
                ws.format("A1:L1", {"textFormat": {"bold": True}})
                ws.format("N1:O1", {"textFormat": {"bold": True}})
            except Exception:
                pass

        # Append trade row
        btc_change = round(btc_close - btc_open, 2) if btc_open and btc_close else 0
        row = [
            timestamp,
            market_title[:60],
            market_type,
            direction,
            round(entry_price * 100, 1),
            round(size_usdc, 2),
            round(shares, 2),
            result,
            round(pnl, 4),
            round(btc_open, 2) if btc_open else "",
            round(btc_close, 2) if btc_close else "",
            btc_change if btc_change else "",
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Logged sniper trade to sheets: %s %s %.0f¢ %s $%.2f",
                     direction, result, entry_price * 100, market_type, pnl)

    except Exception as e:
        logger.error("Sheets logging error: %s", e)


# ── Format ───────────────────────────────────────────────────

def format_session_status(session: SnipeSession) -> str:
    order_emoji = {"live": "⏳", "matched": "✅", "cancelled": "❌"}.get(session.order_status, "❓")
    time_left = max(0, session.market_end_ts - int(time.time()))
    return (
        f"🎯 <b>{session.outcome}</b> @ {session.entry_price*100:.0f}¢ | ${session.size_usdc:.2f}\n"
        f"📌 {session.title[:50]}\n"
        f"📊 {order_emoji} {session.order_status}"
        f"{f' | {session.total_shares:.1f} shares' if session.total_shares > 0 else ''}\n"
        f"⏱ {time_left}s left | 🛡 SL: {session.stop_loss_cents}¢"
    )


def format_auto_status() -> str:
    auto = _auto_sniper
    if not auto:
        return "🎯 Auto-sniper OFF."

    runtime = int(time.time()) - auto.started_at
    hours, mins = runtime // 3600, (runtime % 3600) // 60
    total = auto.wins + auto.losses
    wr = (auto.wins / total * 100) if total > 0 else 0
    sign = "+" if auto.total_pnl >= 0 else ""

    active = ""
    for s in _sessions.values():
        active += f"\n  {format_session_status(s)}"

    return (
        f"🤖 <b>Auto-Sniper {'🟢 ON' if auto.active else '🔴 OFF'}</b>\n\n"
        f"⚙️ {auto.market_type} | Entry: {auto.entry_price*100:.0f}¢ | ${auto.size_usdc:.2f}/trade\n"
        f"⏱ Enter {auto.enter_before_sec}s before close\n"
        f"📊 BTC trigger: ≥{auto.min_btc_move_pct:.2f}% move\n"
        f"🛡 Stop-loss: {auto.stop_loss_cents}¢\n\n"
        f"📈 Trades: {auto.total_trades} | {auto.wins}W/{auto.losses}L ({wr:.0f}%)\n"
        f"💰 P&L: {sign}${auto.total_pnl:.2f}\n"
        f"⏱ {hours}h {mins}m"
        f"{active}"
    )
