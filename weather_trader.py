"""
weather_trader.py — Weather Trading Bot for Polymarket

Strategy:
  1. Find active weather market (London) on Polymarket via Gamma API
  2. Fetch hourly forecast from Open-Meteo (free, no key)
  3. Buy most likely temperature outcome by limit order ($1)
  4. Every 60s: re-check. If forecast changes → sell old, buy new
  5. Hold until resolution → $1 per winning share

Resolution source: Wunderground — London City Airport (EGLC)
Polymarket shows °C outcomes, resolves by °F rounded to whole degrees.

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

# ── Config ───────────────────────────────────────────────

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
_forecast_cache: dict[str, tuple[int, dict]] = {}  # key → (timestamp, forecast)
FORECAST_TTL = 300  # re-fetch forecast every 5 min


# ── Open-Meteo ───────────────────────────────────────────

def fetch_forecast(lat: float, lon: float, target_date: str) -> dict | None:
    """
    Fetch hourly forecast from Open-Meteo.
    target_date: "2026-02-23"
    Returns: {high_c: 13, hourly: [...], source: "Open-Meteo"}
    """
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
                    "high_c": round(high),  # whole °C for Polymarket
                    "high_exact": round(high, 1),
                    "hourly": temps,
                    "source": "Open-Meteo",
                }
    except Exception as e:
        logger.warning("Open-Meteo error: %s", e)
    return None


# ── Polymarket Market ────────────────────────────────────

def find_weather_market(city_key: str) -> dict | None:
    """Find active weather market for city via Gamma API."""
    import requests
    city = CITIES.get(city_key)
    if not city:
        return None
    try:
        # Build possible slugs for today and tomorrow
        now_utc = datetime.now(timezone.utc)
        slugs_to_try = []
        for delta in [0, 1, 2]:
            d = now_utc + timedelta(days=delta)
            month = d.strftime("%B").lower()
            day = d.day
            slug = f"{city['slug_pattern']}{month}-{day}"
            slugs_to_try.append((slug, d.strftime("%Y-%m-%d")))

        for slug, target_date in slugs_to_try:
            # Try fetching event by slug directly
            resp = requests.get(
                f"https://gamma-api.polymarket.com/events/slug/{slug}",
                timeout=15,
            )
            if resp.status_code == 200:
                event = resp.json()
                if not event or event.get("closed"):
                    continue
                markets = event.get("markets", [])
                if not markets:
                    continue

                outcomes = []
                for m in markets:
                    if m.get("closed"):
                        continue
                    cids = m.get("clobTokenIds", [])
                    prices = m.get("outcomePrices", "")
                    try:
                        if isinstance(prices, str):
                            prices = json.loads(prices)
                        price_yes = float(prices[0]) if prices else 0
                    except Exception:
                        price_yes = 0

                    outcomes.append({
                        "question": m.get("question", ""),
                        "condition_id": m.get("conditionId", ""),
                        "token_yes": cids[0] if cids else "",
                        "token_no": cids[1] if len(cids) > 1 else "",
                        "price_yes": price_yes,
                        "closed": m.get("closed", False),
                    })

                if outcomes:
                    logger.info("Found weather market: %s (%d outcomes)", slug, len(outcomes))
                    return {
                        "slug": slug,
                        "title": event.get("title", slug),
                        "outcomes": outcomes,
                        "target_date": target_date,
                    }

        # Fallback: search markets endpoint
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "closed": "false",
                "limit": 50,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            markets = resp.json()
            if isinstance(markets, list):
                for m in markets:
                    q = m.get("question", "").lower()
                    if ("temperature" in q and city["name"].lower() in q
                            and not m.get("closed")):
                        # Found a matching market — extract date
                        date_match = re.search(r'(\w+)\s+(\d+)\??$', m.get("question", ""))
                        target_date = ""
                        if date_match:
                            try:
                                month_str = date_match.group(1)
                                day_str = date_match.group(2)
                                month_num = datetime.strptime(month_str, "%B").month
                                target_date = f"{now_utc.year}-{month_num:02d}-{int(day_str):02d}"
                            except Exception:
                                pass

                        cids = m.get("clobTokenIds", [])
                        prices = m.get("outcomePrices", "")
                        try:
                            if isinstance(prices, str):
                                prices = json.loads(prices)
                            price_yes = float(prices[0]) if prices else 0
                        except Exception:
                            price_yes = 0

                        # This is a single sub-market, need to find parent event
                        event_slug = m.get("eventSlug", "")
                        if event_slug:
                            return find_weather_market_by_event_slug(event_slug, target_date)

                        return {
                            "slug": m.get("slug", ""),
                            "title": m.get("question", ""),
                            "outcomes": [{
                                "question": m.get("question", ""),
                                "condition_id": m.get("conditionId", ""),
                                "token_yes": cids[0] if cids else "",
                                "token_no": cids[1] if len(cids) > 1 else "",
                                "price_yes": price_yes,
                                "closed": False,
                            }],
                            "target_date": target_date,
                        }

    except Exception as e:
        logger.error("Weather market finder: %s", e)
    return None


def find_weather_market_by_event_slug(event_slug: str, target_date: str) -> dict | None:
    """Fetch event by slug to get all sub-markets."""
    import requests
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/events/slug/{event_slug}",
            timeout=15,
        )
        if resp.status_code == 200:
            event = resp.json()
            markets = event.get("markets", [])
            outcomes = []
            for m in markets:
                if m.get("closed"):
                    continue
                cids = m.get("clobTokenIds", [])
                prices = m.get("outcomePrices", "")
                try:
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    price_yes = float(prices[0]) if prices else 0
                except Exception:
                    price_yes = 0
                outcomes.append({
                    "question": m.get("question", ""),
                    "condition_id": m.get("conditionId", ""),
                    "token_yes": cids[0] if cids else "",
                    "token_no": cids[1] if len(cids) > 1 else "",
                    "price_yes": price_yes,
                    "closed": False,
                })
            if outcomes:
                return {
                    "slug": event_slug,
                    "title": event.get("title", event_slug),
                    "outcomes": outcomes,
                    "target_date": target_date,
                }
    except Exception as e:
        logger.error("Event slug fetch: %s", e)
    return None


def match_outcome(market: dict, temp_c: int) -> dict | None:
    """Find outcome matching target temperature."""
    if not market:
        return None

    for o in market.get("outcomes", []):
        q = o["question"]
        nums = re.findall(r'(\d+)\s*°', q)
        if not nums:
            continue
        t = int(nums[0])

        # Exact match (no "below"/"higher")
        if t == temp_c and "below" not in q.lower() and "higher" not in q.lower() and "above" not in q.lower():
            return o

    # Edge: "X°C or below" / "X°C or higher"
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
    from trading import place_limit_buy, place_market_sell, check_order_status, cancel_order
    from sniper import fetch_midprice

    city = CITIES[city_key]
    now = int(time.time())
    pos = _positions.get(city_key)

    # ── Find market (cache-friendly, gamma changes rarely) ─
    if not hasattr(_trade_city, '_market_cache'):
        _trade_city._market_cache = {}
    mc = _trade_city._market_cache.get(city_key)
    if not mc or now - mc[0] > 120:  # refresh market every 2 min
        market = find_weather_market(city_key)
        if market:
            _trade_city._market_cache[city_key] = (now, market)
        else:
            return
    else:
        market = mc[1]

    td = market.get("target_date", "")
    if not td:
        return

    # ── Check fill on existing position (every 5s) ────
    if pos:
        pos.last_check = now
        if not pos.filled and pos.order_id:
            from trading import check_order_status
            st = check_order_status(pos.order_id)
            if st and st.lower() == "matched":
                pos.filled = True
                await _notify(bot,
                    f"✅ <b>WEATHER FILLED</b> | {city['name']} {td}\n"
                    f"🌡 {pos.temp_c}°C @ {pos.buy_price*100:.0f}¢")

    # ── Get forecast (cached, refresh every 5 min) ────
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

    # ── Find matching outcome ─────────────────────────
    outcome = match_outcome(market, forecast_c)
    if not outcome or not outcome["token_yes"]:
        return

    # ── Have position? ────────────────────────────────
    if pos:
        pos.last_check = now

        # Check fill
        if not pos.filled and pos.order_id:
            st = check_order_status(pos.order_id)
            if st and st.lower() == "matched":
                pos.filled = True
                await _notify(bot,
                    f"✅ <b>WEATHER FILLED</b> | {city['name']} {td}\n"
                    f"🌡 {pos.temp_c}°C @ {pos.buy_price*100:.0f}¢")

        # Same forecast → hold
        if forecast_c == pos.temp_c:
            return

        # ── FORECAST CHANGED → SWITCH ─────────────────
        old = pos.temp_c

        if not pos.filled:
            # Not filled yet → just cancel and re-enter
            cancel_order(pos.order_id)
            await _notify(bot,
                f"🔄 <b>SWITCH</b> | {city['name']}\n"
                f"🌡 {old}°C → {forecast_c}°C (cancel unfilled)")
        else:
            # Filled → sell at market, then re-enter
            place_market_sell(pos.token_id, pos.shares, pos.condition_id)
            mid = fetch_midprice(pos.token_id) or pos.buy_price
            pnl = round(pos.shares * mid - pos.cost, 4)
            _stats["switches"] += 1
            await _notify(bot,
                f"🔄 <b>SWITCH</b> | {city['name']}\n"
                f"🌡 {old}°C → {forecast_c}°C\n"
                f"💰 Sold @ {mid*100:.0f}¢ (P&L: ${pnl:.2f})")

        del _positions[city_key]
        pos = None

    # ── ENTER ─────────────────────────────────────────
    token = outcome["token_yes"]
    cid = outcome["condition_id"]

    mid = fetch_midprice(token)
    if not mid:
        mid = outcome.get("price_yes", 0)
    if not mid or mid > 0.90:
        return  # too expensive

    buy_price = round(max(min(mid + 0.02, 0.90), 0.05), 2)

    result = place_limit_buy(token, buy_price, 1.0, cid)
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
        f"📊 Open-Meteo forecast\n"
        f"{'✅ Filled' if is_filled else '⏳ Pending'}")


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


# ── Persistence ──────────────────────────────────────────

def _save_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS weather_trader (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO weather_trader VALUES (?,?)",
                  ("state", json.dumps({"active": _active, **_stats})))
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
        row = c.execute("SELECT value FROM weather_trader WHERE key='state'").fetchone()
        conn.close()
        if row:
            s = json.loads(row[0])
            _active = s.get("active", False)
            for k in _stats:
                _stats[k] = s.get(k, 0)
            _stats["started_at"] = int(time.time())
            return _active
    except Exception as e:
        logger.error("Load weather: %s", e)
    return False


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
    total = w + l
    wr = (w / total * 100) if total > 0 else 0
    sign = "+" if _stats["total_pnl"] >= 0 else ""
    hours = (int(time.time()) - _stats.get("started_at", int(time.time()))) // 3600

    pt = ""
    for ck, p in _positions.items():
        c = CITIES.get(ck, {})
        st = "✅" if p.filled else "⏳"
        pt += (f"\n\n🌡 <b>{c.get('name', ck)}</b> {p.target_date}\n"
               f"  {p.temp_c}°C @ {p.buy_price*100:.0f}¢ {st}\n"
               f"  📌 {p.outcome[:40]}")
    if not pt:
        pt = "\n\n💤 Шукаю ринки..."

    return (f"🌤 <b>Weather Trader 🟢 ON</b>\n\n"
            f"📈 {total}T | {w}W/{l}L ({wr:.0f}%)\n"
            f"💰 {sign}${_stats['total_pnl']:.2f} | 🔄 {_stats['switches']} switches\n"
            f"⏱ {hours}h\n📡 Open-Meteo" + pt)
