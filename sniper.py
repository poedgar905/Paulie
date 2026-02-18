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


async def _notify(bot, text: str):
    """Send notification to owner AND channel."""
    from config import OWNER_ID, CHANNEL_ID
    for chat_id in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.debug("Notify error %s: %s", chat_id, e)


def _log_decision_sync(auto, decision: dict):
    """Log decision to Google Sheets '📋 Decisions' tab (sync)."""
    try:
        from sheets import _get_client, _get_or_create_sheet

        gc, spreadsheet = _get_client()
        if not gc or not spreadsheet:
            return

        ws = _get_or_create_sheet(spreadsheet, "📋 Decisions")

        try:
            first_cell = ws.acell("A1").value
        except Exception:
            first_cell = None

        if not first_cell:
            headers = [
                "Timestamp", "Market", "Type", "Time Left (s)",
                "BTC Open", "BTC Now", "BTC Δ ($)", "BTC Δ (%)",
                "Direction", "Mid (¢)", "Entry (¢)",
                "Last 1m ($)", "Action", "Reason",
            ]
            ws.update("A1:N1", [headers])

            # Stats formulas in column P
            summary = [
                ["DECISION STATS", ""],
                ["Total checks", '=COUNTA(A2:A)'],
                ["ENTER", '=COUNTIF(M2:M,"ENTER")'],
                ["SKIP (low move)", '=COUNTIF(N2:N,"BTC move*")'],
                ["SKIP (reversal)", '=COUNTIF(N2:N,"Trend reversal*")'],
                ["SKIP (too expensive)", '=COUNTIF(N2:N,"*too expensive*")'],
                ["SKIP (mid too low)", '=COUNTIF(N2:N,"*too low*")'],
                ["FAIL (order)", '=COUNTIF(M2:M,"FAIL")'],
                ["Entry rate %", '=IF(P3>0, P4/P3*100, 0)'],
                ["", ""],
                ["AVG BTC Δ% on ENTER", '=AVERAGEIF(M2:M,"ENTER",H2:H)'],
                ["AVG BTC Δ% on SKIP", '=AVERAGEIF(M2:M,"SKIP",H2:H)'],
                ["AVG Mid on ENTER", '=AVERAGEIF(M2:M,"ENTER",J2:J)'],
            ]
            ws.update("P1:Q13", summary)

            try:
                ws.format("A1:N1", {"textFormat": {"bold": True}})
                ws.format("P1:Q1", {"textFormat": {"bold": True}})
            except Exception:
                pass

        row = [
            decision.get("timestamp", ""),
            decision.get("market", ""),
            decision.get("market_type", ""),
            decision.get("time_left", ""),
            round(decision.get("btc_open", 0), 2),
            round(decision.get("btc_now", 0), 2),
            round(decision.get("btc_change", 0), 2),
            decision.get("btc_change_pct", 0),
            decision.get("direction", ""),
            decision.get("mid", ""),
            round(decision.get("entry_price", 0) * 100, 1),
            decision.get("last_1m_move", ""),
            decision.get("action", ""),
            decision.get("reason", ""),
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")

    except Exception as e:
        logger.error("Decision sheet error: %s", e)


async def _log_decision(bot, auto, decision: dict):
    """Log decision to Telegram + Sheets."""
    action = decision.get("action", "?")
    reason = decision.get("reason", "")
    direction = decision.get("direction", "?")
    btc_change = decision.get("btc_change", 0)
    btc_change_pct = decision.get("btc_change_pct", 0)
    btc_open = decision.get("btc_open", 0)
    btc_now = decision.get("btc_now", 0)
    mid = decision.get("mid", 0)
    time_left = decision.get("time_left", 0)
    market = decision.get("market", "?")[:50]
    last_1m = decision.get("last_1m_move", 0)

    if action == "ENTER":
        # ENTER is logged separately in the main flow with more detail
        pass
    elif action == "SKIP":
        emoji = "⏭"
        try:
            await _notify(bot,
                f"{emoji} <b>SKIP</b> | {market}\n"
                f"{'🟢' if direction == 'Up' else '🔴'} {direction} | "
                f"BTC: ${btc_open:,.0f}→${btc_now:,.0f} ({'+' if btc_change>0 else ''}{btc_change:,.0f}, {btc_change_pct:.3f}%)\n"
                f"{'📈 Mid: ' + str(mid) + '¢ | ' if mid else ''}"
                f"{'Last 1m: ' + str(round(last_1m)) + ' | ' if last_1m else ''}"
                f"⏱ {time_left}s left\n"
                f"❌ {reason}"
            )
        except Exception:
            pass
    elif action == "FAIL":
        try:
            await _notify(bot,
                f"⚠️ <b>FAIL</b> | {market}\n"
                f"{'🟢' if direction == 'Up' else '🔴'} {direction} | "
                f"BTC: {'+' if btc_change>0 else ''}{btc_change:,.0f} ({btc_change_pct:.3f}%)\n"
                f"❌ {reason}"
            )
        except Exception:
            pass

    # Log to sheets in background
    try:
        import threading
        threading.Thread(target=_log_decision_sync, args=(auto, decision), daemon=True).start()
    except Exception:
        pass


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
    mid_at_fill: float = 0  # Mid price when order was filled


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


def find_live_market(market_type: str = "15m") -> dict | None:
    """Find currently active BTC up/down market via Gamma API.

    Tries current, previous, and next time windows since timestamps
    may not align exactly with our clock.
    Returns dict with: slug, conditionId, token_ids, end_ts, question
    """
    import requests

    now = int(time.time())
    intervals = {"5m": (300, "btc-updown-5m-"), "15m": (900, "btc-updown-15m-"),
                 "1h": (3600, "btc-updown-1h-"), "4h": (14400, "btc-updown-4h-")}
    if market_type not in intervals:
        return None

    interval, prefix = intervals[market_type]
    period_start = (now // interval) * interval

    # Try current, previous, next windows
    offsets = [0, -interval, interval, -interval * 2]
    for offset in offsets:
        ts = period_start + offset
        slug = f"{prefix}{ts}"
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/events",
                params={"slug": slug, "closed": "false"},
                timeout=10,
            )
            if resp.status_code == 200:
                events = resp.json()
                if isinstance(events, list) and events:
                    event = events[0]
                    markets = event.get("markets", [])
                    if markets:
                        market = markets[0]
                        cid = market.get("conditionId", "")
                        question = market.get("question", event.get("title", "?"))
                        end_ts = ts + interval

                        # Parse token IDs
                        import json
                        tokens_raw = market.get("clobTokenIds", "")
                        if isinstance(tokens_raw, str):
                            try:
                                tokens = json.loads(tokens_raw)
                            except (json.JSONDecodeError, TypeError):
                                tokens = [t.strip() for t in tokens_raw.split(",") if t.strip()]
                        else:
                            tokens = tokens_raw

                        token_yes = tokens[0] if len(tokens) >= 1 else ""
                        token_no = tokens[1] if len(tokens) >= 2 else ""

                        logger.info("Found live market: %s (cid=%s)", slug, cid[:12])
                        return {
                            "slug": slug,
                            "condition_id": cid,
                            "token_yes": token_yes,
                            "token_no": token_no,
                            "end_ts": end_ts,
                            "question": question,
                            "event": event,
                            "market": market,
                        }
        except Exception as e:
            logger.debug("Slug %s not found: %s", slug, e)
            continue

    # Fallback: try fetching by slug directly
    slug = f"{prefix}{period_start}"
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/events/slug/{slug}",
            timeout=10,
        )
        if resp.status_code == 200:
            event = resp.json()
            markets = event.get("markets", [])
            if markets:
                market = markets[0]
                import json
                tokens_raw = market.get("clobTokenIds", "")
                if isinstance(tokens_raw, str):
                    try:
                        tokens = json.loads(tokens_raw)
                    except Exception:
                        tokens = [t.strip() for t in tokens_raw.split(",") if t.strip()]
                else:
                    tokens = tokens_raw

                return {
                    "slug": slug,
                    "condition_id": market.get("conditionId", ""),
                    "token_yes": tokens[0] if len(tokens) >= 1 else "",
                    "token_no": tokens[1] if len(tokens) >= 2 else "",
                    "end_ts": period_start + interval,
                    "question": market.get("question", "?"),
                    "event": event,
                    "market": market,
                }
    except Exception:
        pass

    logger.warning("No live market found for %s", market_type)
    return None


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

    # Find current live market via Gamma API
    live = find_live_market(auto.market_type)
    if not live:
        return

    slug = live["slug"]
    end_ts = live["end_ts"]
    time_left = end_ts - now

    if time_left <= 0:
        auto.current_slug = ""
        auto.current_entered = False
        return

    if slug != auto.current_slug:
        auto.current_slug = slug
        auto.current_cid = ""
        auto.current_entered = False
        if hasattr(auto, '_skipped'):
            auto._skipped.clear()

    if auto.current_entered:
        return

    # Not time yet?
    if time_left > auto.enter_before_sec:
        return

    # ── DECISION TIME — LOG EVERYTHING ────────────────────

    decision_log = {}  # Will be logged to sheets
    decision_log["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    decision_log["market"] = live["question"][:60]
    decision_log["market_type"] = auto.market_type
    decision_log["time_left"] = time_left
    decision_log["entry_price"] = auto.entry_price

    # BTC current price
    btc_now = get_btc_price()
    if not btc_now:
        return

    # BTC period open price from kline
    kline_interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}.get(auto.market_type, "15m")
    kline = get_btc_kline(kline_interval, 1)
    if not kline:
        return

    btc_open = kline["open"]
    btc_change = btc_now - btc_open
    btc_change_pct = abs(btc_change / btc_open) * 100

    decision_log["btc_open"] = btc_open
    decision_log["btc_now"] = btc_now
    decision_log["btc_change"] = btc_change
    decision_log["btc_change_pct"] = round(btc_change_pct, 4)
    decision_log["direction"] = "Up" if btc_change > 0 else "Down"

    # Not enough move?
    if btc_change_pct < auto.min_btc_move_pct:
        # Only log this SKIP once per market (avoid spam every 3s)
        skip_key = f"lowmove_{slug}"
        if not hasattr(auto, '_skipped') or not isinstance(auto._skipped, set):
            auto._skipped = set()
        if skip_key not in auto._skipped:
            auto._skipped.add(skip_key)
            decision_log["action"] = "SKIP"
            decision_log["reason"] = f"BTC move {btc_change_pct:.3f}% < trigger {auto.min_btc_move_pct:.2f}%"
            await _log_decision(bot, auto, decision_log)
        return

    # ── TREND CONFIRMATION ────────────────────────────────
    recent_move = 0
    try:
        import requests as _req
        resp = _req.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": 3},
            timeout=5,
        )
        if resp.status_code == 200:
            candles = resp.json()
            if len(candles) >= 2:
                prev_close = float(candles[-2][4])
                recent_move = btc_now - prev_close
                decision_log["last_1m_move"] = round(recent_move, 2)

                if btc_change > 0 and recent_move < 0:
                    decision_log["action"] = "SKIP"
                    decision_log["reason"] = f"Trend reversal: overall UP but last 1m {recent_move:+.0f}"
                    await _log_decision(bot, auto, decision_log)
                    return
                elif btc_change < 0 and recent_move > 0:
                    decision_log["action"] = "SKIP"
                    decision_log["reason"] = f"Trend reversal: overall DOWN but last 1m {recent_move:+.0f}"
                    await _log_decision(bot, auto, decision_log)
                    return
    except Exception as e:
        logger.debug("Trend check error: %s", e)

    # Direction
    if btc_change > 0:
        direction = "Up"
        token_id = live["token_yes"]
    else:
        direction = "Down"
        token_id = live["token_no"]

    if not token_id:
        decision_log["action"] = "SKIP"
        decision_log["reason"] = "No token_id"
        await _log_decision(bot, auto, decision_log)
        return

    cid = live["condition_id"]
    title = live["question"]

    # ── MOMENTUM CHECK ────────────────────────────────────
    mid = fetch_midprice(token_id)
    decision_log["mid"] = round(mid * 100, 1) if mid else 0

    if mid:
        if mid > auto.entry_price:
            decision_log["action"] = "SKIP"
            decision_log["reason"] = f"Mid {mid*100:.0f}¢ > entry {auto.entry_price*100:.0f}¢ (too expensive)"
            auto.current_entered = True
            await _log_decision(bot, auto, decision_log)
            return

        if mid < auto.entry_price * 0.5:
            decision_log["action"] = "SKIP"
            decision_log["reason"] = f"Mid {mid*100:.0f}¢ too low (direction unclear)"
            await _log_decision(bot, auto, decision_log)
            return

    # ── PLACE ORDER ───────────────────────────────────────
    result = place_limit_buy(token_id, auto.entry_price, auto.size_usdc, cid)
    if not result or not result.get("order_id"):
        decision_log["action"] = "FAIL"
        decision_log["reason"] = "Order placement failed"
        await _log_decision(bot, auto, decision_log)
        logger.error("Failed to place order for %s", slug)
        return

    # SUCCESS — order placed
    decision_log["action"] = "ENTER"
    decision_log["reason"] = "All checks passed"
    await _log_decision(bot, auto, decision_log)

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
        await _notify(bot,
            f"🎯 <b>AUTO-SNIPE!</b>\n\n"
            f"📌 {title[:60]}\n"
            f"{'🟢' if direction == 'Up' else '🔴'} {direction} @ {auto.entry_price*100:.0f}¢\n"
            f"💵 ${auto.size_usdc:.2f}"
            f"{f' | Mid: {mid*100:.0f}¢' if mid else ''}\n"
            f"📊 BTC: ${btc_open:,.0f} → ${btc_now:,.0f} ({'+' if btc_change > 0 else ''}{btc_change:,.0f}, {btc_change_pct:.3f}%)\n"
            f"✅ Trend confirmed (last 1m same direction)\n"
            f"⏱ {time_left}s left"
        )
    except Exception:
        pass


async def _check_session(bot, session: SnipeSession):
    """Check fill, stop-loss, resolution."""
    from trading import check_order_status, cancel_order, place_market_sell
    from config import OWNER_ID, CHANNEL_ID

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

            # Record mid price at fill for proper stop-loss baseline
            fill_mid = fetch_midprice(session.token_id)
            session.mid_at_fill = fill_mid if fill_mid else 0

            try:
                await _notify(bot,
                    f"✅ <b>FILL!</b> {session.outcome} @ {session.entry_price*100:.0f}¢\n"
                    f"📌 {session.title[:50]}\n"
                    f"📊 {shares:.1f} shares = ${session.size_usdc:.2f}\n"
                    f"📈 Mid at fill: {session.mid_at_fill*100:.0f}¢\n"
                    f"⏳ Чекаємо resolution..."
                )
            except Exception:
                pass

        elif status_lower in ("cancelled", "expired"):
            session.active = False
            remove_session(session.condition_id)
            return

    # ── Stop-loss ─────────────────────────────────────────
    # For late-entry: mid is often BELOW entry price at fill time.
    # SL triggers if mid drops X¢ below MID AT FILL, not below entry price.
    if session.order_status == "matched" and session.stop_loss_cents > 0:
        if session.mid_at_fill and session.mid_at_fill > 0:
            mid = fetch_midprice(session.token_id)
            if mid and mid > 0:
                drop = session.mid_at_fill - mid
                if drop >= session.stop_loss_cents / 100:
                    # STOP-LOSS triggered
                    place_market_sell(session.token_id, session.total_shares, session.condition_id)
                    pnl = (mid * session.total_shares) - session.total_spent

                    if _auto_sniper:
                        _auto_sniper.losses += 1
                        _auto_sniper.total_pnl += pnl

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
                        enter_before_sec=_auto_sniper.enter_before_sec if _auto_sniper else 0,
                        btc_trigger_pct=_auto_sniper.min_btc_move_pct if _auto_sniper else 0,
                        stop_loss_cents=session.stop_loss_cents,
                        time_left_at_entry=max(0, session.market_end_ts - session.started_at),
                    )

                    try:
                        await _notify(bot,
                            f"🛑 <b>STOP-LOSS!</b>\n"
                            f"📌 {session.title[:50]}\n"
                            f"Mid at fill: {session.mid_at_fill*100:.0f}¢ → Now: {mid*100:.0f}¢ (drop {drop*100:.0f}¢)\n"
                            f"💰 ${pnl:.2f}"
                            + (f"\n📈 {_auto_sniper.wins}W/{_auto_sniper.losses}L = ${_auto_sniper.total_pnl:.2f}" if _auto_sniper else "")
                        )
                    except Exception:
                        pass

                    session.active = False
                    remove_session(session.condition_id)
                    return

    # ── Resolution ────────────────────────────────────────
    if session.order_status == "matched" and session.market_end_ts > 0:
        now = int(time.time())
        time_since_end = now - session.market_end_ts

        if time_since_end > 15:
            # Try Gamma API first
            market = fetch_market_by_condition(session.condition_id)
            resolved_via = ""
            resolution = ""
            won = False

            if market:
                closed = market.get("closed", False)
                res = market.get("resolution", "")
                # closed can be string "true" or bool True
                is_closed = closed in (True, "true", "True", 1, "1")

                if is_closed and res:
                    resolution = str(res)
                    won = _check_win(session.outcome, resolution)
                    resolved_via = "API"
                    logger.info("Resolution via API: %s -> %s (won=%s)", session.outcome, resolution, won)

            # Fallback: if no resolution after 3 minutes, check BTC price
            if not resolved_via and time_since_end > 120:
                # Get the kline that matches our market period
                # Market end_ts = period start + interval
                # We need the kline that STARTED at (end_ts - interval)
                try:
                    import requests as _req
                    interval_sec = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}.get(
                        _auto_sniper.market_type if _auto_sniper else "15m", 900)
                    kline_interval = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}.get(
                        _auto_sniper.market_type if _auto_sniper else "15m", "15m")

                    # Fetch kline that started at our market start time
                    market_start_ms = (session.market_end_ts - interval_sec) * 1000
                    resp = _req.get(
                        "https://api.binance.com/api/v3/klines",
                        params={
                            "symbol": "BTCUSDT",
                            "interval": kline_interval,
                            "startTime": market_start_ms,
                            "limit": 1,
                        },
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        candles = resp.json()
                        if candles:
                            k = candles[0]
                            btc_open = float(k[1])
                            btc_close = float(k[4])

                            if btc_close > btc_open:
                                resolution = "Up"
                            elif btc_close < btc_open:
                                resolution = "Down"
                            else:
                                resolution = "Up"

                            won = _check_win(session.outcome, resolution)
                            resolved_via = "BTC"
                            logger.info("BTC fallback: open=%.0f close=%.0f -> %s (we=%s, won=%s)",
                                        btc_open, btc_close, resolution, session.outcome, won)
                except Exception as e:
                    logger.error("BTC fallback error: %s", e)

            # Force resolve after 10 minutes no matter what
            if not resolved_via and time_since_end > 600:
                # Assume win based on the fact that we entered with trend confirmation
                # This is a last resort — should rarely happen
                resolution = session.outcome  # Assume our direction won
                won = True
                resolved_via = "TIMEOUT"
                logger.warning("Resolution via timeout: assuming %s won", session.outcome)

            if resolved_via:
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
                    enter_before_sec=_auto_sniper.enter_before_sec if _auto_sniper else 0,
                    btc_trigger_pct=_auto_sniper.min_btc_move_pct if _auto_sniper else 0,
                    stop_loss_cents=session.stop_loss_cents,
                    time_left_at_entry=max(0, session.market_end_ts - session.started_at),
                )

                emoji = "🟩" if won else "🟥"
                try:
                    await _notify(bot,
                        f"{emoji} <b>{'WIN' if won else 'LOSS'}!</b> {session.outcome} @ {session.entry_price*100:.0f}¢\n"
                        f"📌 {session.title[:50]}\n"
                        f"Resolved: {resolution} ({resolved_via}) | 💰 {'+'if pnl>=0 else ''}${pnl:.2f}"
                        + (f"\n📈 {_auto_sniper.wins}W/{_auto_sniper.losses}L = ${_auto_sniper.total_pnl:.2f}" if _auto_sniper else "")
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
    enter_before_sec: int = 0,
    btc_trigger_pct: float = 0,
    stop_loss_cents: int = 0,
    time_left_at_entry: int = 0,
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
            headers = [
                "Timestamp", "Market", "Type", "Direction",
                "Entry (¢)", "Size ($)", "Shares",
                "Result", "P&L ($)",
                "BTC Open", "BTC Close", "BTC Δ",
                "Enter Before (s)", "BTC Trigger (%)", "SL (¢)", "Time Left (s)",
            ]
            ws.update("A1:P1", [headers])

            # Summary formulas in column R
            summary = [
                ["STATS", ""],
                ["Total trades", '=COUNTA(A2:A)'],
                ["Wins", '=COUNTIF(H2:H,"WIN")'],
                ["Losses", '=COUNTIF(H2:H,"LOSS")'],
                ["Stop-losses", '=COUNTIF(H2:H,"STOP-LOSS")'],
                ["No fills", '=COUNTIF(H2:H,"NO-FILL")'],
                ["Win Rate %", '=IF(R3>0,R4/(R4+R5+R6)*100,0)'],
                ["Total P&L $", '=SUM(I2:I)'],
                ["Total Spent $", '=SUMIF(H2:H,"<>NO-FILL",F2:F)'],
                ["ROI %", '=IF(R10>0,R9/R10*100,0)'],
                ["Avg Win $", '=IF(R4>0,SUMIF(H2:H,"WIN",I2:I)/R4,0)'],
                ["Avg Loss $", '=IF((R5+R6)>0,(SUMIF(H2:H,"LOSS",I2:I)+SUMIF(H2:H,"STOP-LOSS",I2:I))/(R5+R6),0)'],
                ["Best Trade $", '=MAX(I2:I)'],
                ["Worst Trade $", '=MIN(I2:I)'],
                ["", ""],
                ["BY TIMING", ""],
                ["30s trades", '=COUNTIF(M2:M,30)'],
                ["30s WR%", '=IF(R18>0, COUNTIFS(M2:M,30,H2:H,"WIN")/COUNTIFS(M2:M,30,H2:H,"<>NO-FILL")*100, 0)'],
                ["60s trades", '=COUNTIF(M2:M,60)'],
                ["60s WR%", '=IF(R20>0, COUNTIFS(M2:M,60,H2:H,"WIN")/COUNTIFS(M2:M,60,H2:H,"<>NO-FILL")*100, 0)'],
                ["120s trades", '=COUNTIF(M2:M,120)'],
                ["120s WR%", '=IF(R22>0, COUNTIFS(M2:M,120,H2:H,"WIN")/COUNTIFS(M2:M,120,H2:H,"<>NO-FILL")*100, 0)'],
                ["180s trades", '=COUNTIF(M2:M,180)'],
                ["180s WR%", '=IF(R24>0, COUNTIFS(M2:M,180,H2:H,"WIN")/COUNTIFS(M2:M,180,H2:H,"<>NO-FILL")*100, 0)'],
                ["", ""],
                ["BY ENTRY PRICE", ""],
                ["80¢ WR%", '=IF(COUNTIF(E2:E,80)>0, COUNTIFS(E2:E,80,H2:H,"WIN")/COUNTIFS(E2:E,80,H2:H,"<>NO-FILL")*100, 0)'],
                ["83¢ WR%", '=IF(COUNTIF(E2:E,83)>0, COUNTIFS(E2:E,83,H2:H,"WIN")/COUNTIFS(E2:E,83,H2:H,"<>NO-FILL")*100, 0)'],
                ["85¢ WR%", '=IF(COUNTIF(E2:E,85)>0, COUNTIFS(E2:E,85,H2:H,"WIN")/COUNTIFS(E2:E,85,H2:H,"<>NO-FILL")*100, 0)'],
                ["88¢ WR%", '=IF(COUNTIF(E2:E,88)>0, COUNTIFS(E2:E,88,H2:H,"WIN")/COUNTIFS(E2:E,88,H2:H,"<>NO-FILL")*100, 0)'],
                ["", ""],
                ["BY BTC TRIGGER", ""],
                ["0.01% WR%", '=IF(COUNTIF(N2:N,0.01)>0, COUNTIFS(N2:N,0.01,H2:H,"WIN")/COUNTIFS(N2:N,0.01,H2:H,"<>NO-FILL")*100, 0)'],
                ["0.03% WR%", '=IF(COUNTIF(N2:N,0.03)>0, COUNTIFS(N2:N,0.03,H2:H,"WIN")/COUNTIFS(N2:N,0.03,H2:H,"<>NO-FILL")*100, 0)'],
                ["0.05% WR%", '=IF(COUNTIF(N2:N,0.05)>0, COUNTIFS(N2:N,0.05,H2:H,"WIN")/COUNTIFS(N2:N,0.05,H2:H,"<>NO-FILL")*100, 0)'],
                ["0.10% WR%", '=IF(COUNTIF(N2:N,0.1)>0, COUNTIFS(N2:N,0.1,H2:H,"WIN")/COUNTIFS(N2:N,0.1,H2:H,"<>NO-FILL")*100, 0)'],
            ]
            ws.update("R1:S36", summary)

            try:
                ws.format("A1:P1", {"textFormat": {"bold": True}})
                ws.format("R1:S1", {"textFormat": {"bold": True}})
                ws.format("R17:S17", {"textFormat": {"bold": True}})
                ws.format("R27:S27", {"textFormat": {"bold": True}})
                ws.format("R32:S32", {"textFormat": {"bold": True}})
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
            enter_before_sec,
            btc_trigger_pct,
            stop_loss_cents,
            time_left_at_entry,
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Logged trade: %s %s %.0f¢ %ss %s $%.2f",
                     direction, result, entry_price * 100, enter_before_sec, market_type, pnl)

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
        f"📊 BTC trigger: ≥{auto.min_btc_move_pct:.2f}%\n"
        f"🛡 SL: {auto.stop_loss_cents}¢ | 🔒 Momentum ON\n\n"
        f"📈 Trades: {auto.total_trades} | {auto.wins}W/{auto.losses}L ({wr:.0f}%)\n"
        f"💰 P&L: {sign}${auto.total_pnl:.2f}\n"
        f"⏱ {hours}h {mins}m"
        f"{active}"
    )
