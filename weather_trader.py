"""
weather_trader.py — Weather Trading Bot for Polymarket

Strategy:
  1. Find active weather market (London) via slug
  2. Fetch forecast from Open-Meteo (free, no key)
  3. Buy most likely outcome by limit ($1)
  4. Every 5s check orders; every 5min re-check forecast
  5. If forecast changes → cancel/sell old → buy new
  6. Hold winning position until resolution

Command: /weather_trade [start|stop|status]
"""
import asyncio
import json
import logging
import re
import time
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

CITIES = {
    "london": {
        "name": "London",
        "lat": 51.505, "lon": -0.118,
        "slug_pattern": "highest-temperature-in-london-on-",
    },
}

_active = False
_stats = {
    "wins": 0, "losses": 0, "total_pnl": 0.0,
    "total_trades": 0, "switches": 0, "started_at": 0,
}
_forecast_cache: dict[str, tuple[int, dict]] = {}
_market_cache: dict[str, tuple[int, dict]] = {}
FORECAST_TTL = 300  # 5 min
MARKET_TTL = 120    # 2 min


# ── Open-Meteo ───────────────────────────────────────────

def fetch_forecast(lat: float, lon: float, target_date: str) -> dict | None:
    import requests
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m",
                "start_date": target_date,
                "end_date": target_date,
                "timezone": "auto",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            temps = data.get("hourly", {}).get("temperature_2m", [])
            if temps:
                high = max(temps)
                return {
                    "high_c": round(high),
                    "high_exact": round(high, 1),
                    "hourly": temps,
                }
    except Exception as e:
        logger.warning("Open-Meteo error: %s", e)
    return None


# ── Polymarket ───────────────────────────────────────────

def _parse_market_outcomes(markets: list) -> list:
    """Parse market list into clean outcomes."""
    outcomes = []
    for m in markets:
        if m.get("closed"):
            continue
        cids = m.get("clobTokenIds", [])
        if isinstance(cids, str):
            try:
                cids = json.loads(cids)
            except Exception:
                cids = []
        prices = m.get("outcomePrices", "")
        try:
            if isinstance(prices, str):
                prices = json.loads(prices)
            price_yes = float(prices[0]) if prices else 0
        except Exception:
            price_yes = 0

        token_yes = cids[0] if len(cids) > 0 else ""
        token_no = cids[1] if len(cids) > 1 else ""

        if token_yes:  # skip if no token
            outcomes.append({
                "question": m.get("question", ""),
                "condition_id": m.get("conditionId", ""),
                "token_yes": token_yes,
                "token_no": token_no,
                "price_yes": price_yes,
            })
    return outcomes


def find_weather_market(city_key: str) -> dict | None:
    import requests
    city = CITIES.get(city_key)
    if not city:
        return None

    now = int(time.time())
    cached = _market_cache.get(city_key)
    if cached and now - cached[0] < MARKET_TTL:
        return cached[1]

    try:
        now_utc = datetime.now(timezone.utc)
        for delta in [0, 1, 2]:
            d = now_utc + timedelta(days=delta)
            slug = f"{city['slug_pattern']}{d.strftime('%B').lower()}-{d.day}-{d.year}"
            td = d.strftime("%Y-%m-%d")

            resp = requests.get(
                f"https://gamma-api.polymarket.com/events/slug/{slug}",
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            event = resp.json()
            if not event or event.get("closed"):
                continue
            markets = event.get("markets", [])
            outcomes = _parse_market_outcomes(markets)
            if outcomes:
                result = {
                    "slug": slug,
                    "title": event.get("title", slug),
                    "outcomes": outcomes,
                    "target_date": td,
                }
                _market_cache[city_key] = (now, result)
                logger.info("Found weather market: %s (%d outcomes)", slug, len(outcomes))
                return result
    except Exception as e:
        logger.error("Weather market finder: %s", e)
    return None


def match_outcome(market: dict, temp_c: int) -> dict | None:
    if not market:
        return None
    for o in market.get("outcomes", []):
        q = o["question"]
        nums = re.findall(r'(\d+)\s*°', q)
        if not nums:
            continue
        t = int(nums[0])
        if t == temp_c and "below" not in q.lower() and "higher" not in q.lower() and "above" not in q.lower():
            return o
    # Edge cases
    for o in market.get("outcomes", []):
        q = o["question"].lower()
        nums = re.findall(r'(\d+)\s*°', q)
        if not nums:
            continue
        t = int(nums[0])
        if ("below" in q or "lower" in q) and temp_c <= t:
            return o
        if ("higher" in q or "above" in q) and temp_c >= t:
            return o
    return None


# ── Position ─────────────────────────────────────────────

@dataclass
class Position:
    city: str = ""
    slug: str = ""
    target_date: str = ""
    outcome: str = ""
    condition_id: str = ""
    token_id: str = ""
    temp_c: int = 0
    order_id: str = ""
    buy_price: float = 0
    shares: float = 0
    cost: float = 0
    filled: bool = False
    switching: bool = False  # True while selling old position
    entered_at: int = 0
    last_check: int = 0

_positions: dict[str, Position] = {}


# ── Notify ───────────────────────────────────────────────

async def _notify(bot, text):
    from config import OWNER_ID, CHANNEL_ID
    for cid in [OWNER_ID, CHANNEL_ID]:
        try:
            await bot.send_message(chat_id=cid, text=text, parse_mode="HTML")
        except Exception:
            pass


# ── Main Loop ────────────────────────────────────────────

async def weather_checker(bot):
    logger.info("Weather trader started (5s)")
    await asyncio.sleep(5)
    if _load_state():
        await _notify(bot, "🌤 <b>Weather Trader restored</b>")
    while True:
        try:
            if _active:
                for ck in CITIES:
                    await _trade_city(bot, ck)
        except Exception as e:
            logger.error("Weather error: %s", e)
        await asyncio.sleep(5)


async def _trade_city(bot, city_key: str):
    from trading import place_limit_buy, place_limit_sell, cancel_order, check_order_status
    from trading import get_conditional_balance, debug_balance_info
    from sniper import fetch_midprice

    city = CITIES[city_key]
    now = int(time.time())
    pos = _positions.get(city_key)

    # Throttle: if position has recent last_check, skip
    if pos and now - pos.last_check < 4:
        return

    # ── Find market ───────────────────────────────────
    market = find_weather_market(city_key)
    if not market or not market.get("target_date"):
        return
    td = market["target_date"]

    # ── If we have position on DIFFERENT date → it's expired ─
    if pos and pos.target_date != td:
        logger.info("Position expired: %s (market now %s)", pos.target_date, td)
        # Old market resolved or closed, clear position
        if not pos.filled and pos.order_id:
            cancel_order(pos.order_id)
        del _positions[city_key]
        pos = None

    # ── Check fill on existing position ───────────────
    if pos and not pos.filled and pos.order_id:
        st = check_order_status(pos.order_id)
        if st and st.lower() == "matched":
            pos.filled = True
            _save_state()
            await _notify(bot,
                f"✅ <b>WEATHER FILLED</b> | {city['name']} {td}\n"
                f"🌡 {pos.temp_c}°C @ {pos.buy_price*100:.0f}¢")
        elif now - pos.entered_at > 300 and not pos.filled:
            # Not filled in 5 min → cancel and retry at better price
            cancel_order(pos.order_id)
            await _notify(bot,
                f"⏰ <b>WEATHER CANCEL</b> | Not filled 5min\n"
                f"🌡 {pos.temp_c}°C @ {pos.buy_price*100:.0f}¢ — will retry")
            del _positions[city_key]
            pos = None

    # ── Get forecast (cached 5 min) ───────────────────
    cache_key = f"{city_key}:{td}"
    cached = _forecast_cache.get(cache_key)
    if cached and now - cached[0] < FORECAST_TTL:
        fc = cached[1]
    else:
        fc = fetch_forecast(city["lat"], city["lon"], td)
        if fc:
            _forecast_cache[cache_key] = (now, fc)
        else:
            return
    forecast_c = fc["high_c"]

    # ── Match outcome ─────────────────────────────────
    outcome = match_outcome(market, forecast_c)
    if not outcome or not outcome["token_yes"]:
        return

    # ── Have position with same forecast → hold ───────
    if pos and forecast_c == pos.temp_c:
        return

    # ── FORECAST CHANGED (or no position) → ACT ──────
    if pos:
        old = pos.temp_c
        if not pos.filled:
            # Easy: just cancel unfilled order
            cancel_order(pos.order_id)
            await _notify(bot,
                f"🔄 <b>SWITCH</b> | {city['name']}\n"
                f"🌡 {old}°C → {forecast_c}°C (cancelled unfilled)")
            del _positions[city_key]
            pos = None
        else:
            # Hard: need to sell filled position
            # Use limit sell at current mid for 0 commission
            mid = fetch_midprice(pos.token_id)
            if not mid or mid < 0.02:
                mid = 0.02

            sell_price = round(max(mid - 0.02, 0.01), 2)

            # Get REAL balance from chain — don't trust stored shares
            real_balance = get_conditional_balance(pos.token_id)
            if real_balance and real_balance > 0:
                sell_size = round(real_balance, 2)
                logger.info("Real balance: %s shares (stored: %s)", real_balance, pos.shares)
            else:
                # Fallback: sell 90% of stored shares
                sell_size = round(pos.shares * 0.90, 2)
                logger.warning("Could not get real balance, using 90%%: %s", sell_size)

            if sell_size < 0.1:
                sell_size = 0.1

            # Log debug info
            dbg = debug_balance_info(pos.token_id)
            logger.info("SELL debug: %s", dbg)

            logger.info("SELL attempt: token=%s price=%s size=%s (had %s)",
                        pos.token_id[:20], sell_price, sell_size, pos.shares)

            sell_res = place_limit_sell(
                pos.token_id, sell_price, sell_size, pos.condition_id)

            if sell_res and sell_res.get("order_id"):
                # Check if instant fill
                resp = sell_res.get("response", {})
                if resp.get("status") == "matched":
                    pnl = round(sell_size * sell_price - pos.cost, 4)
                    _stats["switches"] += 1
                    _stats["total_pnl"] += pnl
                    await _notify(bot,
                        f"🔄 <b>SWITCH SOLD</b> | {city['name']}\n"
                        f"🌡 {old}°C → {forecast_c}°C\n"
                        f"💰 Sold {sell_size} sh @ {sell_price*100:.0f}¢ (P&L: ${pnl:+.2f})")
                    del _positions[city_key]
                    pos = None
                else:
                    # Sell placed but not filled yet — wait for next cycle
                    pos.switching = True
                    pos.order_id = sell_res["order_id"]
                    await _notify(bot,
                        f"🔄 <b>SWITCH SELLING</b> | {city['name']}\n"
                        f"🌡 {old}°C → {forecast_c}°C\n"
                        f"📤 Sell limit {sell_size} sh @ {sell_price*100:.0f}¢ pending...")
                    _save_state()
                    return  # Don't enter new position yet
            else:
                # Sell failed — don't spam, mark and skip for 60s
                logger.error("Switch sell failed for %s", city_key)
                pos.last_check = now + 55  # skip next 55 seconds
                if not hasattr(pos, '_sell_fail_notified'):
                    pos._sell_fail_notified = True
                    await _notify(bot,
                        f"⚠️ <b>SWITCH SELL FAIL</b> | {city['name']}\n"
                        f"🌡 Tried {old}°C → {forecast_c}°C\n"
                        f"Will retry in 60s")
                return

    # If position is in switching state, check if sell filled
    if pos and pos.switching:
        st = check_order_status(pos.order_id)
        if st and st.lower() == "matched":
            mid = fetch_midprice(pos.token_id) or pos.buy_price
            pnl = round(pos.shares * mid - pos.cost, 4)
            _stats["switches"] += 1
            _stats["total_pnl"] += pnl
            await _notify(bot,
                f"✅ <b>SWITCH SOLD</b> | {city['name']}\n"
                f"💰 P&L: ${pnl:+.2f}")
            del _positions[city_key]
            pos = None
        else:
            return  # Still waiting for sell to fill

    # ── ENTER new position ────────────────────────────
    if pos:
        return  # Still have position, don't double-enter

    token = outcome["token_yes"]
    cid = outcome["condition_id"]

    mid = fetch_midprice(token)
    if not mid:
        mid = outcome.get("price_yes", 0)
    if not mid or mid > 0.90:
        return

    buy_price = round(max(min(mid + 0.02, 0.90), 0.05), 2)

    # Need min 5 shares. At buy_price, cost = 5 * buy_price
    # Send enough USDC for 5 shares
    amount_usdc = round(5.0 * buy_price + 0.10, 2)  # +10¢ buffer

    result = place_limit_buy(token, buy_price, amount_usdc, cid)
    if not result or not result.get("order_id"):
        return

    shares = result.get("size", 5.0)
    cost = round(shares * buy_price, 2)
    is_filled = result.get("response", {}).get("status") == "matched"

    _positions[city_key] = Position(
        city=city_key, slug=market["slug"], target_date=td,
        outcome=outcome["question"], condition_id=cid, token_id=token,
        temp_c=forecast_c, order_id=result["order_id"],
        buy_price=buy_price, shares=shares, cost=cost,
        filled=is_filled, entered_at=now, last_check=now,
    )
    _stats["total_trades"] += 1
    _save_state()

    await _notify(bot,
        f"🌤 <b>WEATHER BUY</b> | {city['name']} {td}\n"
        f"🌡 {forecast_c}°C (exact: {fc['high_exact']}°C)\n"
        f"💰 {buy_price*100:.0f}¢ × {shares} sh = ${cost:.2f}\n"
        f"{'✅ Filled' if is_filled else '⏳ Pending'}")


# ── Persistence ──────────────────────────────────────────

def _save_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS weather_trader (key TEXT PRIMARY KEY, value TEXT)")

        # Save stats
        c.execute("INSERT OR REPLACE INTO weather_trader VALUES (?,?)",
                  ("stats", json.dumps(_stats)))

        # Save positions
        pos_data = {}
        for ck, p in _positions.items():
            pos_data[ck] = {
                "city": p.city, "slug": p.slug, "target_date": p.target_date,
                "outcome": p.outcome, "condition_id": p.condition_id,
                "token_id": p.token_id, "temp_c": p.temp_c,
                "order_id": p.order_id, "buy_price": p.buy_price,
                "shares": p.shares, "cost": p.cost, "filled": p.filled,
                "switching": p.switching, "entered_at": p.entered_at,
            }
        c.execute("INSERT OR REPLACE INTO weather_trader VALUES (?,?)",
                  ("positions", json.dumps(pos_data)))

        c.execute("INSERT OR REPLACE INTO weather_trader VALUES (?,?)",
                  ("active", json.dumps(_active)))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Save weather: %s", e)


def _load_state():
    from config import DB_PATH
    global _active
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS weather_trader (key TEXT PRIMARY KEY, value TEXT)")

        # Load active
        row = c.execute("SELECT value FROM weather_trader WHERE key='active'").fetchone()
        if row:
            _active = json.loads(row[0])

        # Load stats
        row = c.execute("SELECT value FROM weather_trader WHERE key='stats'").fetchone()
        if row:
            s = json.loads(row[0])
            for k in _stats:
                _stats[k] = s.get(k, 0)
            _stats["started_at"] = int(time.time())

        # Load positions
        row = c.execute("SELECT value FROM weather_trader WHERE key='positions'").fetchone()
        if row:
            pos_data = json.loads(row[0])
            for ck, pd in pos_data.items():
                _positions[ck] = Position(**pd, last_check=int(time.time()))

        conn.close()
        return _active
    except Exception as e:
        logger.error("Load weather: %s", e)
    return False


# ── Sheets ───────────────────────────────────────────────

def _log_trade(pos, result, pnl):
    import threading
    def _w():
        try:
            from sheets import _get_client, _get_or_create_sheet
            gc, sp = _get_client()
            if not gc or not sp:
                return
            ws = _get_or_create_sheet(sp, "🌤 Weather")
            try:
                f = ws.acell("A1").value
            except Exception:
                f = None
            if not f:
                ws.update("A1:I1", [["Timestamp", "City", "Date", "Forecast°C",
                    "Buy¢", "Shares", "P&L$", "Result", "Switches"]],
                    value_input_option="USER_ENTERED")
            ws.append_row([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                pos.city, pos.target_date, pos.temp_c,
                round(pos.buy_price*100, 1), pos.shares,
                round(pnl, 4), result, _stats["switches"],
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            logger.error("Weather sheets: %s", e)
    threading.Thread(target=_w, daemon=True).start()


# ── Control ──────────────────────────────────────────────

def start_weather():
    global _active
    _active = True
    _stats["started_at"] = int(time.time())
    _save_state()

def stop_weather():
    global _active
    _active = False
    from trading import cancel_order
    for p in _positions.values():
        if not p.filled and p.order_id:
            cancel_order(p.order_id)
    _positions.clear()
    _save_state()

def is_weather_active():
    return _active

def get_weather_status():
    if not _active:
        return "🌤 Weather Trader: OFF"
    w, l = _stats["wins"], _stats["losses"]
    total = _stats["total_trades"]
    sign = "+" if _stats["total_pnl"] >= 0 else ""
    hours = (int(time.time()) - _stats.get("started_at", int(time.time()))) // 3600

    pt = ""
    for ck, p in _positions.items():
        c = CITIES.get(ck, {})
        if p.switching:
            st = "🔄 Switching"
        elif p.filled:
            st = "✅ Holding"
        else:
            st = "⏳ Pending"
        pt += (f"\n\n🌡 <b>{c.get('name', ck)}</b> {p.target_date}\n"
               f"  {p.temp_c}°C @ {p.buy_price*100:.0f}¢ | {st}\n"
               f"  📌 {p.outcome[:40]}")
    if not pt:
        pt = "\n\n💤 Шукаю ринки..."

    return (f"🌤 <b>Weather Trader 🟢 ON</b>\n\n"
            f"📈 {total} trades | 🔄 {_stats['switches']} switches\n"
            f"💰 {sign}${_stats['total_pnl']:.2f}\n"
            f"⏱ {hours}h | 📡 Open-Meteo" + pt)
