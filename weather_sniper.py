"""
Weather Sniper — auto-trades multi-outcome Polymarket events.
User sends a Polymarket event URL → bot monitors all outcomes,
places limit orders on the cheapest outcomes, cancels losers when one fills.
"""
import asyncio
import json
import logging
import time
import re
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Active weather snipers ──────────────────────────────
_weather_snipers: dict[str, "WeatherSniper"] = {}  # key = event_slug


@dataclass
class OutcomeOrder:
    """Tracks one outcome and its limit order."""
    outcome_name: str = ""
    token_id: str = ""
    condition_id: str = ""
    order_id: str = ""
    order_status: str = ""  # live, matched, cancelled
    price: float = 0  # limit price
    shares: float = 0
    cost: float = 0
    market_prob: float = 0  # current probability


@dataclass
class WeatherSniper:
    """One event being monitored."""
    active: bool = True
    event_slug: str = ""
    event_url: str = ""
    event_title: str = ""
    max_price: float = 0.65  # max limit price
    size_usdc: float = 2.0   # per outcome
    enter_hours_before: float = 10  # enter X hours before close
    
    # Outcomes tracking
    outcomes: list = field(default_factory=list)  # list of OutcomeOrder
    
    # State
    orders_placed: bool = False  # already placed orders?
    filled_outcome: str = ""  # which outcome got filled
    filled_at: int = 0
    resolved: bool = False
    result: str = ""  # WIN / LOSS
    pnl: float = 0
    
    # Stats
    started_at: int = 0
    event_end_ts: int = 0  # when event closes (unix timestamp)


# ── Parse Polymarket URL ────────────────────────────────

def parse_polymarket_url(url: str) -> dict | None:
    """Extract event slug from Polymarket URL."""
    # https://polymarket.com/event/highest-temperature-in-london-on-february-20
    # or https://polymarket.com/event/highest-temperature-in-london-on-february-20?tid=...
    match = re.search(r'polymarket\.com/event/([a-zA-Z0-9\-]+)', url)
    if match:
        return {"slug": match.group(1), "type": "event"}
    
    # Also handle market URLs
    match = re.search(r'polymarket\.com/market/([a-zA-Z0-9\-]+)', url)
    if match:
        return {"slug": match.group(1), "type": "market"}
    
    return None


def fetch_event_data(slug: str) -> dict | None:
    """Fetch full event data from Gamma API."""
    import requests
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": slug},
            timeout=15,
        )
        if resp.status_code == 200:
            events = resp.json()
            if isinstance(events, list) and events:
                return events[0]
    except Exception as e:
        logger.error("Fetch event error: %s", e)
    return None


def parse_event_markets(event: dict) -> list[dict]:
    """Parse all markets (outcomes) from an event."""
    results = []
    markets = event.get("markets", [])
    for m in markets:
        question = m.get("question", "")
        condition_id = m.get("conditionId", "")
        
        # Parse token IDs
        tokens_raw = m.get("clobTokenIds", "")
        if isinstance(tokens_raw, str):
            try:
                tokens = json.loads(tokens_raw)
            except (json.JSONDecodeError, TypeError):
                tokens = [t.strip() for t in tokens_raw.split(",") if t.strip()]
        else:
            tokens = tokens_raw or []
        
        # Parse outcomes
        outcomes_raw = m.get("outcomes", "")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                outcomes = [o.strip() for o in outcomes_raw.split(",") if o.strip()]
        else:
            outcomes = outcomes_raw or []
        
        # Parse prices
        prices_raw = m.get("outcomePrices", "")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except (json.JSONDecodeError, TypeError):
                prices = []
        else:
            prices = prices_raw or []
        
        token_yes = tokens[0] if len(tokens) >= 1 else ""
        outcome_yes = outcomes[0] if len(outcomes) >= 1 else "Yes"
        price_yes = float(prices[0]) if len(prices) >= 1 else 0
        
        closed = m.get("closed", False)
        if closed in (True, "true", "True", 1, "1"):
            continue
        
        results.append({
            "question": question,
            "condition_id": condition_id,
            "token_id": token_yes,
            "outcome": outcome_yes,
            "price": price_yes,
            "closed": False,
            "end_date": m.get("endDate", ""),
            "resolution": m.get("resolution", ""),
        })
    
    return results


# ── Sniper Control ──────────────────────────────────────

def start_weather_sniper(
    event_url: str,
    max_price: float = 0.65,
    size_usdc: float = 2.0,
    enter_hours_before: float = 10,
) -> WeatherSniper | None:
    """Start monitoring a Polymarket event."""
    parsed = parse_polymarket_url(event_url)
    if not parsed:
        return None
    
    slug = parsed["slug"]
    if slug in _weather_snipers:
        return _weather_snipers[slug]  # Already monitoring
    
    event = fetch_event_data(slug)
    if not event:
        return None
    
    title = event.get("title", slug)
    markets = parse_event_markets(event)
    if not markets:
        return None
    
    # Determine event end time from first market's endDate
    end_ts = 0
    for m in markets:
        end_date = m.get("end_date", "")
        if end_date:
            try:
                from datetime import datetime, timezone
                # endDate format: "2026-02-20T00:00:00Z" or similar
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                end_ts = int(dt.timestamp())
                break
            except Exception:
                pass
    
    # Create outcome trackers
    outcomes = []
    for m in markets:
        outcomes.append(OutcomeOrder(
            outcome_name=m["question"],
            token_id=m["token_id"],
            condition_id=m["condition_id"],
            market_prob=m["price"],
        ))
    
    sniper = WeatherSniper(
        active=True,
        event_slug=slug,
        event_url=event_url,
        event_title=title,
        max_price=max_price,
        size_usdc=size_usdc,
        enter_hours_before=enter_hours_before,
        outcomes=outcomes,
        started_at=int(time.time()),
        event_end_ts=end_ts,
    )
    
    _weather_snipers[slug] = sniper
    _save_weather_config()
    logger.info("Weather sniper started: %s (%d outcomes, end=%d, enter %dh before)",
                title, len(outcomes), end_ts, enter_hours_before)
    return sniper


def stop_weather_sniper(slug: str) -> WeatherSniper | None:
    """Stop monitoring an event."""
    from trading import cancel_order
    sniper = _weather_snipers.pop(slug, None)
    if sniper:
        sniper.active = False
        # Cancel all live orders
        for o in sniper.outcomes:
            if o.order_id and o.order_status == "live":
                try:
                    cancel_order(o.order_id)
                except Exception:
                    pass
    _save_weather_config()
    return sniper


def stop_all_weather() -> list[WeatherSniper]:
    stopped = []
    for slug in list(_weather_snipers.keys()):
        s = stop_weather_sniper(slug)
        if s:
            stopped.append(s)
    return stopped


def get_all_weather_snipers() -> list[WeatherSniper]:
    return list(_weather_snipers.values())


# ── Main Loop ───────────────────────────────────────────

async def weather_checker(bot):
    """Background loop for weather snipers — runs every 30 seconds."""
    logger.info("Weather checker started (30s)")
    await asyncio.sleep(10)
    
    # Restore saved snipers
    count = _load_weather_config()
    if count > 0:
        try:
            from config import OWNER_ID, CHANNEL_ID
            lines = [f"  • {s.event_title[:50]}" for s in _weather_snipers.values()]
            text = f"🌤 <b>Auto-restored {count} weather sniper(s)</b>\n" + "\n".join(lines)
            for cid in [OWNER_ID, CHANNEL_ID]:
                try:
                    await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
                except Exception:
                    pass
        except Exception:
            pass
    
    while True:
        try:
            for slug, sniper in list(_weather_snipers.items()):
                if not sniper.active:
                    continue
                try:
                    await _check_weather_sniper(bot, sniper)
                except Exception as e:
                    logger.error("Weather sniper %s error: %s", slug[:20], e)
                await asyncio.sleep(1)
        except Exception as e:
            logger.error("Weather loop error: %s", e)
        
        await asyncio.sleep(30)


async def _check_weather_sniper(bot, sniper: WeatherSniper):
    """Check one weather sniper — wait for right time, place order on leader, check resolution."""
    from trading import place_limit_buy, check_order_status, cancel_order
    from sniper import fetch_midprice
    
    # ── Already resolved? ─────────────────────────────────
    if sniper.resolved:
        return
    
    # ── If filled — check resolution ──────────────────────
    if sniper.filled_outcome:
        await _check_weather_resolution(bot, sniper)
        return
    
    # ── If orders placed — check fills ────────────────────
    if sniper.orders_placed:
        for o in sniper.outcomes:
            if o.order_id and o.order_status == "live":
                status = check_order_status(o.order_id)
                if status and status.lower() == "matched":
                    o.order_status = "matched"
                    o.shares = round(sniper.size_usdc / o.price, 2)
                    o.cost = sniper.size_usdc
                    sniper.filled_outcome = o.outcome_name
                    sniper.filled_at = int(time.time())
                    
                    # Cancel all other orders
                    for other in sniper.outcomes:
                        if other.outcome_name != o.outcome_name and other.order_id and other.order_status == "live":
                            try:
                                cancel_order(other.order_id)
                                other.order_status = "cancelled"
                            except Exception:
                                pass
                    
                    await _weather_notify(bot,
                        f"✅ <b>FILL!</b> {sniper.event_title[:50]}\n"
                        f"📌 {o.outcome_name}\n"
                        f"💰 {o.shares} shares @ {o.price*100:.0f}¢ = ${o.cost:.2f}\n"
                        f"⏳ Тримаємо до resolution..."
                    )
                    _save_weather_config()
                    return
        return
    
    # ── Update probabilities ──────────────────────────────
    event = fetch_event_data(sniper.event_slug)
    if not event:
        return
    
    markets = parse_event_markets(event)
    for m in markets:
        for o in sniper.outcomes:
            if o.condition_id == m["condition_id"]:
                o.market_prob = m["price"]
    
    # ── Check timing ────────────────────────────────────────
    now = int(time.time())
    
    if sniper.event_end_ts <= 0:
        # Try to get end_ts from market data
        for m in markets:
            end_date = m.get("end_date", "")
            if end_date:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    sniper.event_end_ts = int(dt.timestamp())
                    break
                except Exception:
                    pass
    
    hours_left = (sniper.event_end_ts - now) / 3600 if sniper.event_end_ts > 0 else 999
    
    if hours_left < 0:
        await _check_weather_resolution(bot, sniper)
        return
    
    # ── Find the leader ───────────────────────────────────
    ranked = sorted(sniper.outcomes, key=lambda o: o.market_prob, reverse=True)
    
    if not ranked:
        return
    
    leader = ranked[0]
    outcome_info = ", ".join(f"{o.outcome_name[:15]}={o.market_prob*100:.0f}%" for o in ranked[:4])
    
    # ── ENTRY CONDITIONS ──────────────────────────────────
    # Both must be true:
    #   1. Leader probability >= 55% (confident enough)
    #   2. Hours left <= enter_hours_before (default 15h)
    # This catches the sweet spot: leader is clear but still cheap
    
    min_prob = 0.55  # leader must be at least 55%
    
    leader_ready = leader.market_prob >= min_prob
    time_ready = hours_left <= sniper.enter_hours_before
    affordable = leader.market_prob <= sniper.max_price
    
    if not time_ready:
        # Too far from close — just monitor
        return
    
    if not leader_ready:
        # In time window but no clear leader yet — wait
        if not hasattr(sniper, '_notified_waiting') or not sniper._notified_waiting:
            sniper._notified_waiting = True
            await _weather_notify(bot,
                f"⏳ <b>MONITOR</b> | {sniper.event_title[:50]}\n"
                f"🏆 Лідер: {leader.outcome_name} ({leader.market_prob*100:.0f}%)\n"
                f"📊 Потрібно ≥55% для входу\n"
                f"⏱ {hours_left:.1f}h до закриття\n"
                f"📊 {outcome_info}"
            )
        return
    
    if not affordable:
        # Leader is confident but already too expensive
        if not hasattr(sniper, '_notified_expensive') or not sniper._notified_expensive:
            sniper._notified_expensive = True
            await _weather_notify(bot,
                f"💸 <b>TOO LATE</b> | {sniper.event_title[:50]}\n"
                f"🏆 Лідер: {leader.outcome_name} ({leader.market_prob*100:.0f}%)\n"
                f"💰 Ціна {leader.market_prob*100:.0f}¢ > max {sniper.max_price*100:.0f}¢\n"
                f"⏱ {hours_left:.1f}h до закриття\n"
                f"📊 {outcome_info}"
            )
        return
    
    # ── SWEET SPOT! Leader >= 55%, affordable, in time window → ENTER!
    try:
        result = place_limit_buy(
            leader.token_id, sniper.max_price, sniper.size_usdc, leader.condition_id
        )
        if result and result.get("order_id"):
            leader.order_id = result["order_id"]
            leader.order_status = "live"
            leader.price = sniper.max_price
            sniper.orders_placed = True
            
            await _weather_notify(bot,
                f"🎯 <b>ORDER!</b> {sniper.event_title[:50]}\n"
                f"📌 {leader.outcome_name} @ {sniper.max_price*100:.0f}¢\n"
                f"📊 Prob: {leader.market_prob*100:.0f}% (лідер ≥55%)\n"
                f"💰 ${sniper.size_usdc:.2f}\n"
                f"⏱ {hours_left:.1f}h до закриття\n"
                f"📊 {outcome_info}"
            )
            _save_weather_config()
    except Exception as e:
        logger.error("Weather order error: %s", e)


async def _check_weather_resolution(bot, sniper: WeatherSniper):
    """Check if the filled outcome resolved."""
    import requests
    
    event = fetch_event_data(sniper.event_slug)
    if not event:
        return
    
    markets = event.get("markets", [])
    all_closed = True
    won = False
    
    for m in markets:
        closed = m.get("closed", False)
        if closed not in (True, "true", "True", 1, "1"):
            all_closed = False
            continue
        
        resolution = m.get("resolution", "")
        question = m.get("question", "")
        
        # Check if our filled outcome won
        for o in sniper.outcomes:
            if o.condition_id == m.get("conditionId", "") and o.outcome_name == sniper.filled_outcome:
                if resolution in ("1", "Yes", "yes"):
                    won = True
    
    if not all_closed:
        return  # Not resolved yet
    
    # Calculate P&L
    filled = None
    for o in sniper.outcomes:
        if o.outcome_name == sniper.filled_outcome:
            filled = o
            break
    
    if not filled:
        return
    
    if won:
        pnl = filled.shares * 1.0 - filled.cost
        sniper.result = "WIN"
    else:
        pnl = -filled.cost
        sniper.result = "LOSS"
    
    sniper.pnl = pnl
    sniper.resolved = True
    sniper.active = False
    
    emoji = "🟩" if won else "🟥"
    sign = "+" if pnl >= 0 else ""
    await _weather_notify(bot,
        f"{emoji} <b>{sniper.result}!</b> {sniper.event_title[:50]}\n"
        f"📌 {sniper.filled_outcome}\n"
        f"💰 {sign}${pnl:.2f} ({filled.shares} shares @ {filled.price*100:.0f}¢)"
    )
    
    # Log to sheets
    _log_weather_trade(sniper)
    
    # Remove from active
    _weather_snipers.pop(sniper.event_slug, None)
    _save_weather_config()


# ── Notifications ───────────────────────────────────────

async def _weather_notify(bot, text: str):
    from config import OWNER_ID, CHANNEL_ID
    for chat_id in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        except Exception as e:
            logger.debug("Weather notify error: %s", e)


# ── Google Sheets logging ───────────────────────────────

def _log_weather_trade(sniper: WeatherSniper):
    """Log resolved weather trade to Sheets."""
    try:
        from sheets import _get_client, _get_or_create_sheet
        from datetime import datetime, timezone
        
        gc, spreadsheet = _get_client()
        if not gc or not spreadsheet:
            return
        
        ws = _get_or_create_sheet(spreadsheet, "🌤 Weather")
        
        try:
            first_cell = ws.acell("A1").value
        except Exception:
            first_cell = None
        
        if not first_cell:
            headers = [
                "Timestamp", "Event", "Outcome", "Price (¢)",
                "Shares", "Cost ($)", "Result", "P&L ($)",
                "Outcomes Count",
            ]
            ws.update("A1:I1", [headers], value_input_option="USER_ENTERED")
            
            summary = [
                ["WEATHER STATS", ""],
                ["Total trades", '=COUNTA(A2:A)'],
                ["Wins", '=COUNTIF(G2:G,"WIN")'],
                ["Losses", '=COUNTIF(G2:G,"LOSS")'],
                ["Win Rate %", '=IF(K3>0,K4/(K4+K5)*100,0)'],
                ["Total P&L", '=SUM(H2:H)'],
                ["Avg Win", '=IFERROR(AVERAGEIF(G2:G,"WIN",H2:H),0)'],
                ["Avg Loss", '=IFERROR(AVERAGEIF(G2:G,"LOSS",H2:H),0)'],
            ]
            ws.update("K1:L8", summary, value_input_option="USER_ENTERED")
            
            try:
                ws.format("A1:I1", {"textFormat": {"bold": True}})
                ws.format("K1:K1", {"textFormat": {"bold": True}})
            except Exception:
                pass
        
        filled = None
        for o in sniper.outcomes:
            if o.outcome_name == sniper.filled_outcome:
                filled = o
                break
        
        row = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            sniper.event_title[:60],
            sniper.filled_outcome,
            round(filled.price * 100, 1) if filled else 0,
            round(filled.shares, 2) if filled else 0,
            round(filled.cost, 2) if filled else 0,
            sniper.result,
            round(sniper.pnl, 4),
            len(sniper.outcomes),
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        logger.error("Weather sheets error: %s", e)


# ── Persistence via tracker.db ──────────────────────────

def _save_weather_config():
    """Save weather snipers to tracker.db."""
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS weather_sniper (
            event_slug TEXT PRIMARY KEY,
            config TEXT NOT NULL
        )""")
        c.execute("DELETE FROM weather_sniper")
        for s in _weather_snipers.values():
            if s.active:
                cfg = json.dumps({
                    "event_slug": s.event_slug,
                    "event_url": s.event_url,
                    "event_title": s.event_title,
                    "max_price": s.max_price,
                    "size_usdc": s.size_usdc,
                    "enter_hours_before": s.enter_hours_before,
                    "event_end_ts": s.event_end_ts,
                    "orders_placed": s.orders_placed,
                    "filled_outcome": s.filled_outcome,
                    "outcomes": [
                        {
                            "outcome_name": o.outcome_name,
                            "token_id": o.token_id,
                            "condition_id": o.condition_id,
                            "order_id": o.order_id,
                            "order_status": o.order_status,
                            "price": o.price,
                            "shares": o.shares,
                            "cost": o.cost,
                        }
                        for o in s.outcomes
                    ],
                })
                c.execute("INSERT OR REPLACE INTO weather_sniper (event_slug, config) VALUES (?, ?)",
                          (s.event_slug, cfg))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Save weather config error: %s", e)


def _load_weather_config() -> int:
    """Load saved weather snipers from tracker.db."""
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS weather_sniper (
            event_slug TEXT PRIMARY KEY,
            config TEXT NOT NULL
        )""")
        rows = c.execute("SELECT config FROM weather_sniper").fetchall()
        conn.close()
        
        count = 0
        for (cfg_json,) in rows:
            cfg = json.loads(cfg_json)
            outcomes = []
            for o_cfg in cfg.get("outcomes", []):
                outcomes.append(OutcomeOrder(
                    outcome_name=o_cfg["outcome_name"],
                    token_id=o_cfg["token_id"],
                    condition_id=o_cfg["condition_id"],
                    order_id=o_cfg.get("order_id", ""),
                    order_status=o_cfg.get("order_status", ""),
                    price=o_cfg.get("price", 0),
                    shares=o_cfg.get("shares", 0),
                    cost=o_cfg.get("cost", 0),
                ))
            
            s = WeatherSniper(
                active=True,
                event_slug=cfg["event_slug"],
                event_url=cfg.get("event_url", ""),
                event_title=cfg.get("event_title", ""),
                max_price=cfg.get("max_price", 0.65),
                size_usdc=cfg.get("size_usdc", 2.0),
                enter_hours_before=cfg.get("enter_hours_before", 10),
                event_end_ts=cfg.get("event_end_ts", 0),
                orders_placed=cfg.get("orders_placed", False),
                outcomes=outcomes,
                filled_outcome=cfg.get("filled_outcome", ""),
                started_at=int(time.time()),
            )
            _weather_snipers[cfg["event_slug"]] = s
            count += 1
        return count
    except Exception as e:
        logger.error("Load weather config error: %s", e)
        return 0


# ── Status formatting ───────────────────────────────────

def format_weather_status() -> str:
    if not _weather_snipers:
        return "🌤 Weather snipers: немає активних."
    
    parts = []
    for s in _weather_snipers.values():
        now = int(time.time())
        hours_left = (s.event_end_ts - now) / 3600 if s.event_end_ts > 0 else -1
        
        # Timing status
        if s.filled_outcome:
            timing = "🎯 Filled — чекаємо resolution"
        elif s.orders_placed:
            timing = "🔵 Ордер розміщений — чекаємо fill"
        elif hours_left > s.enter_hours_before:
            timing = f"⏳ Чекаємо ({hours_left:.1f}h left, window at ≤{s.enter_hours_before:.0f}h)"
        else:
            # In window — check leader
            ranked = sorted(s.outcomes, key=lambda o: o.market_prob, reverse=True)
            leader_prob = ranked[0].market_prob * 100 if ranked else 0
            if leader_prob >= 55:
                timing = f"🟡 Готовий! Лідер {leader_prob:.0f}% ≥ 55% ({hours_left:.1f}h left)"
            else:
                timing = f"👀 Моніторю ({hours_left:.1f}h left, лідер {leader_prob:.0f}% < 55%)"
        
        outcome_lines = []
        for o in s.outcomes:
            status_icon = "⬜"
            if o.order_status == "matched":
                status_icon = "✅"
            elif o.order_status == "live":
                status_icon = "🔵"
            elif o.order_status == "cancelled":
                status_icon = "❌"
            
            prob = f"{o.market_prob*100:.0f}%" if o.market_prob else "?"
            outcome_lines.append(
                f"  {status_icon} {o.outcome_name[:30]} | {prob}"
                + (f" | order @ {o.price*100:.0f}¢" if o.order_id else "")
            )
        
        parts.append(
            f"🌤 <b>{s.event_title[:50]}</b>\n"
            f"  💰 ${s.size_usdc:.0f} | Max: {s.max_price*100:.0f}¢ | Enter: {s.enter_hours_before:.0f}h before\n"
            f"  {timing}\n"
            + "\n".join(outcome_lines)
        )
    
    return "\n\n".join(parts)
