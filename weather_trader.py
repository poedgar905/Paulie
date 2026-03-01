"""
weather_trader.py — Weather Trading Bot v2: Orderbook Flow Strategy

Instead of forecast data, watches the orderbook for volume shifts.
When big money flows into an outcome → follow it.

Strategy:
  1. Find active weather market (London temperature)
  2. Poll orderbook for ALL outcomes every 5s (direct HTTP, no py-clob-client)
  3. Track bid/ask volume changes over time
  4. When volume surge detected on an outcome → buy it
  5. When our outcome loses momentum → sell and switch

Command: /weather_trade [start|stop|status]
"""
import asyncio
import json
import logging
import re
import time
import sqlite3
import requests
from dataclasses import dataclass
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
CLOB_API = "https://clob.polymarket.com"


# ── Orderbook Polling (direct HTTP, no py-clob-client) ───

def fetch_orderbook(token_id: str) -> dict | None:
    """Fetch raw orderbook from CLOB API. No auth needed."""
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=8,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.warning("Orderbook fetch error: %s", e)
    return None


def parse_book_volume(book: dict) -> dict:
    """Extract total bid/ask volume from orderbook."""
    bid_vol = 0.0
    ask_vol = 0.0
    best_bid = 0.0
    best_ask = 1.0

    for b in book.get("bids", []):
        size = float(b.get("size", 0))
        price = float(b.get("price", 0))
        bid_vol += size
        if price > best_bid:
            best_bid = price

    for a in book.get("asks", []):
        size = float(a.get("size", 0))
        price = float(a.get("price", 0))
        ask_vol += size
        if price < best_ask:
            best_ask = price

    return {
        "bid_vol": round(bid_vol, 2),
        "ask_vol": round(ask_vol, 2),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": round((best_bid + best_ask) / 2, 4) if best_bid > 0 else 0,
    }


# ── Orderbook Snapshots ─────────────────────────────────

@dataclass
class OutcomeState:
    question: str
    token_id: str
    condition_id: str
    temp_c: int
    bid_vol: float = 0
    ask_vol: float = 0
    mid: float = 0
    best_bid: float = 0
    best_ask: float = 0
    prev_bid_vol: float = 0
    prev_mid: float = 0
    bid_momentum: float = 0
    price_momentum: float = 0
    last_update: int = 0


_market_cache: dict[str, tuple[int, dict]] = {}
_outcome_states: dict[str, dict[str, OutcomeState]] = {}
MARKET_TTL = 120


# ── Market Discovery ─────────────────────────────────────

def _parse_market_outcomes(markets: list) -> list:
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
        if token_yes:
            q = m.get("question", "")
            nums = re.findall(r'(\d+)\s*°', q)
            temp_c = int(nums[0]) if nums else 0
            outcomes.append({
                "question": q,
                "condition_id": m.get("conditionId", ""),
                "token_yes": token_yes,
                "price_yes": price_yes,
                "temp_c": temp_c,
            })
    return outcomes


def find_weather_market(city_key: str) -> dict | None:
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
    switching: bool = False
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


# ── Signal Detection ─────────────────────────────────────

def detect_signal(city_key: str, market: dict) -> dict | None:
    """
    Analyze orderbook across all outcomes.
    Returns best outcome to bet on based on volume flow.
    """
    outcomes = market.get("outcomes", [])
    if not outcomes:
        return None

    if city_key not in _outcome_states:
        _outcome_states[city_key] = {}
    states = _outcome_states[city_key]

    now = int(time.time())
    best_outcome = None
    best_score = 0

    for o in outcomes:
        token = o["token_yes"]
        if not token:
            continue

        book = fetch_orderbook(token)
        if not book:
            continue

        vol = parse_book_volume(book)

        if token not in states:
            states[token] = OutcomeState(
                question=o["question"],
                token_id=token,
                condition_id=o["condition_id"],
                temp_c=o.get("temp_c", 0),
            )

        st = states[token]

        # Save previous
        st.prev_bid_vol = st.bid_vol
        st.prev_mid = st.mid

        # Update
        st.bid_vol = vol["bid_vol"]
        st.ask_vol = vol["ask_vol"]
        st.mid = vol["mid"]
        st.best_bid = vol["best_bid"]
        st.best_ask = vol["best_ask"]

        # Momentum (exponential moving average)
        if st.prev_bid_vol > 0:
            bid_change = st.bid_vol - st.prev_bid_vol
            st.bid_momentum = st.bid_momentum * 0.7 + bid_change * 0.3
        if st.prev_mid > 0:
            price_change = st.mid - st.prev_mid
            st.price_momentum = st.price_momentum * 0.7 + price_change * 0.3

        st.last_update = now

        # Score = market price + momentum signals
        price_score = st.mid * 100
        momentum_score = st.bid_momentum * 2
        price_trend = st.price_momentum * 200

        score = price_score + momentum_score + price_trend

        # Only 5¢ - 85¢ range
        if st.mid < 0.05 or st.mid > 0.85:
            continue

        if score > best_score:
            best_score = score
            best_outcome = {
                "question": o["question"],
                "token_yes": token,
                "condition_id": o["condition_id"],
                "temp_c": o.get("temp_c", 0),
                "mid": st.mid,
                "bid_vol": st.bid_vol,
                "bid_momentum": round(st.bid_momentum, 2),
                "price_momentum": round(st.price_momentum, 4),
                "score": round(score, 2),
            }

    return best_outcome


# ── Main Loop ────────────────────────────────────────────

async def weather_checker(bot):
    logger.info("Weather trader v2 (orderbook flow) started")
    await asyncio.sleep(5)
    if _load_state():
        await _notify(bot, "🌤 <b>Weather v2 restored</b>\n📊 Orderbook flow")
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
    from trading import get_conditional_balance

    city = CITIES[city_key]
    now = int(time.time())
    pos = _positions.get(city_key)

    # ── Find market
    market = find_weather_market(city_key)
    if not market or not market.get("target_date"):
        return
    td = market["target_date"]

    # ── Expired position
    if pos and pos.target_date != td:
        if not pos.filled and pos.order_id:
            cancel_order(pos.order_id)
        del _positions[city_key]
        pos = None

    # ── Check fill
    if pos and not pos.filled and pos.order_id:
        st = check_order_status(pos.order_id)
        if st and st.lower() == "matched":
            pos.filled = True
            _save_state()
            await _notify(bot,
                f"✅ <b>FILLED</b> | {city['name']} {td}\n"
                f"🌡 {pos.temp_c}°C @ {pos.buy_price*100:.0f}¢")
        elif now - pos.entered_at > 300 and not pos.filled:
            cancel_order(pos.order_id)
            del _positions[city_key]
            pos = None

    # ── Switching state
    if pos and pos.switching:
        st = check_order_status(pos.order_id)
        if st and st.lower() == "matched":
            pnl = round(pos.shares * pos.buy_price * 0.9 - pos.cost, 4)
            _stats["switches"] += 1
            _stats["total_pnl"] += pnl
            await _notify(bot,
                f"✅ <b>SOLD</b> | {city['name']}\n💰 P&L: ${pnl:+.2f}")
            del _positions[city_key]
            pos = None
        elif now - pos.last_check > 60:
            cancel_order(pos.order_id)
            del _positions[city_key]
            pos = None
        else:
            return

    # ── Detect signal
    signal = detect_signal(city_key, market)
    if not signal:
        return

    # ── Have position?
    if pos:
        if signal["token_yes"] == pos.token_id:
            return  # Same → hold

        # Different outcome → switch
        if not pos.filled:
            cancel_order(pos.order_id)
            await _notify(bot,
                f"🔄 <b>SWITCH</b> | {city['name']}\n"
                f"🌡 {pos.temp_c}°C → {signal['temp_c']}°C (unfilled)")
            del _positions[city_key]
            pos = None
        else:
            real_bal = get_conditional_balance(pos.token_id)
            sell_size = round(real_bal, 2) if real_bal and real_bal > 0 else round(pos.shares * 0.90, 2)
            if sell_size < 0.1:
                sell_size = 0.1

            pos_state = _outcome_states.get(city_key, {}).get(pos.token_id)
            sell_price = round(max((pos_state.mid if pos_state else 0.05) - 0.02, 0.01), 2)

            sell_res = place_limit_sell(pos.token_id, sell_price, sell_size, pos.condition_id)
            if sell_res and sell_res.get("order_id"):
                resp = sell_res.get("response", {})
                if resp.get("status") == "matched":
                    pnl = round(sell_size * sell_price - pos.cost, 4)
                    _stats["switches"] += 1
                    _stats["total_pnl"] += pnl
                    await _notify(bot,
                        f"🔄 <b>SOLD</b> | {city['name']}\n"
                        f"🌡 {pos.temp_c}°C → {signal['temp_c']}°C | ${pnl:+.2f}")
                    del _positions[city_key]
                    pos = None
                else:
                    pos.switching = True
                    pos.order_id = sell_res["order_id"]
                    pos.last_check = now
                    _save_state()
                    return
            else:
                if not hasattr(pos, '_fail_n'):
                    pos._fail_n = True
                    await _notify(bot, f"⚠️ <b>SELL FAIL</b> | {city['name']}")
                pos.last_check = now + 55
                return

    # ── ENTER
    if pos:
        return

    token = signal["token_yes"]
    cid = signal["condition_id"]
    mid = signal["mid"]

    if mid > 0.85 or mid < 0.03:
        return

    buy_price = round(max(min(mid + 0.02, 0.90), 0.05), 2)
    amount_usdc = round(max(5.0 * buy_price, 1.0), 2)

    result = place_limit_buy(token, buy_price, amount_usdc, cid)
    if not result or not result.get("order_id"):
        return

    shares = result.get("size", 5.0)
    cost = round(shares * buy_price, 2)
    is_filled = result.get("response", {}).get("status") == "matched"

    _positions[city_key] = Position(
        city=city_key, slug=market["slug"], target_date=td,
        outcome=signal["question"], condition_id=cid, token_id=token,
        temp_c=signal["temp_c"], order_id=result["order_id"],
        buy_price=buy_price, shares=shares, cost=cost,
        filled=is_filled, entered_at=now, last_check=now,
    )
    _stats["total_trades"] += 1
    _save_state()

    await _notify(bot,
        f"🌤 <b>BUY</b> | {city['name']} {td}\n"
        f"🌡 {signal['temp_c']}°C @ {buy_price*100:.0f}¢\n"
        f"📊 Score: {signal['score']} | Mom: {signal['bid_momentum']}\n"
        f"💰 ${cost:.2f} ({shares} sh)\n"
        f"{'✅ Filled' if is_filled else '⏳ Pending'}")


# ── Persistence ──────────────────────────────────────────

def _save_state():
    from config import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS weather_trader (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO weather_trader VALUES (?,?)",
                  ("stats", json.dumps(_stats)))
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
        row = c.execute("SELECT value FROM weather_trader WHERE key='active'").fetchone()
        if row:
            _active = json.loads(row[0])
        row = c.execute("SELECT value FROM weather_trader WHERE key='stats'").fetchone()
        if row:
            s = json.loads(row[0])
            for k in _stats:
                _stats[k] = s.get(k, 0)
            _stats["started_at"] = int(time.time())
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
    _outcome_states.clear()
    _save_state()

def is_weather_active():
    return _active

def get_weather_status():
    if not _active:
        return "🌤 Weather Trader: OFF"
    total = _stats["total_trades"]
    sign = "+" if _stats["total_pnl"] >= 0 else ""
    hours = (int(time.time()) - _stats.get("started_at", int(time.time()))) // 3600

    pt = ""
    for ck, p in _positions.items():
        c = CITIES.get(ck, {})
        st = "🔄 Switch" if p.switching else ("✅ Hold" if p.filled else "⏳ Pend")
        pt += (f"\n\n🌡 <b>{c.get('name', ck)}</b> {p.target_date}\n"
               f"  {p.temp_c}°C @ {p.buy_price*100:.0f}¢ | {st}")

    ob_info = ""
    for ck, states in _outcome_states.items():
        top = sorted(states.values(), key=lambda s: s.mid, reverse=True)[:3]
        for s in top:
            if s.mid > 0.03:
                arrow = "🟢" if s.bid_momentum > 0 else "🔴"
                ob_info += f"\n  {arrow} {s.temp_c}°C: {s.mid*100:.0f}¢ bids:{s.bid_vol:.0f} mom:{s.bid_momentum:+.1f}"

    if not pt:
        pt = "\n\n💤 Scanning..."

    return (f"🌤 <b>Weather v2 🟢</b>\n📊 Orderbook Flow\n\n"
            f"📈 {total} trades | 🔄 {_stats['switches']} sw\n"
            f"💰 {sign}${_stats['total_pnl']:.2f} | ⏱ {hours}h"
            + pt
            + ("\n\n<b>📊 Book:</b>" + ob_info if ob_info else ""))
