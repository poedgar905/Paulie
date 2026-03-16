"""
Sniper 90¢ — Elon Tweet Markets
Places limit buy orders at 90¢ on top 3 probable ranges, 48h before event end.
Monitors and cancels if ranges shift. Notifies on fill.

Strategy:
- Fetch all active Elon tweet events from Gamma API
- For enabled events: find top 3 ranges by current price (highest = most probable)
- Place $2 limit buy at 90¢ on each
- Every 10 min: check if top 3 shifted → cancel old, place new
- When order fills → notify channel
- 48h before end → activate, at end → cleanup
"""
import asyncio
import logging
import time
import json
import requests
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("sniper90")

GAMMA_API = "https://gamma-api.polymarket.com"
SNIPE_PRICE = 0.90  # Buy at 90¢
SNIPE_AMOUNT = 2.0   # $2 per position
CHECK_INTERVAL = 600  # 10 minutes
HOURS_BEFORE_END = 48  # Activate 48h before end


# ── Fetch Elon tweet events ──────────────────────────────────────

def fetch_elon_events() -> list[dict]:
    """Fetch all active Elon Musk tweet events from Gamma API."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/events",
            params={"active": "true", "closed": "false", "limit": "100"},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error("Gamma events error: %s", resp.status_code)
            return []

        events = resp.json()
        if not isinstance(events, list):
            return []

        # Filter for Elon tweet events
        elon_events = []
        for ev in events:
            title = (ev.get("title") or ev.get("slug") or "").lower()
            if "elon" in title and "tweet" in title:
                elon_events.append(ev)

        return elon_events
    except Exception as e:
        logger.error("Fetch elon events error: %s", e)
        return []


def fetch_event_markets(event_slug: str) -> list[dict]:
    """Fetch all markets (ranges) for an event."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/events/slug/{event_slug}",
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        event = resp.json()
        markets = event.get("markets", [])
        return markets if isinstance(markets, list) else []
    except Exception as e:
        logger.error("Fetch event markets error: %s", e)
        return []


def get_market_prices(markets: list[dict]) -> list[dict]:
    """Get current prices for all markets in an event. Returns sorted by price desc."""
    priced = []
    for m in markets:
        try:
            prices_str = m.get("outcomePrices", "[]")
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if prices else 0

            outcomes_str = m.get("outcomes", "[]")
            outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str

            tokens_str = m.get("clobTokenIds", "[]")
            tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
            yes_token = tokens[0] if tokens else ""

            priced.append({
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "condition_id": m.get("conditionId", ""),
                "token_id": yes_token,
                "yes_price": yes_price,
                "outcome": outcomes[0] if outcomes else "Yes",
                "end_date": m.get("endDate", ""),
                "neg_risk": m.get("negRisk", False),
            })
        except Exception as e:
            logger.error("Price parse error for %s: %s", m.get("question", "?")[:30], e)

    # Sort by price descending (highest = most probable)
    priced.sort(key=lambda x: x["yes_price"], reverse=True)
    return priced


# ── Order management ─────────────────────────────────────────────

# Track active sniper orders: {token_id: order_id}
_active_orders: dict[str, dict] = {}


def place_snipe_order(token_id: str, condition_id: str) -> dict | None:
    """Place limit buy at 90¢."""
    from trading import _get_client, get_neg_risk
    import math

    client = _get_client()
    if not client:
        return None

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        price = SNIPE_PRICE
        size = math.ceil(SNIPE_AMOUNT / price * 100) / 100
        if size < 5:
            size = 5.0

        neg_risk = get_neg_risk(condition_id)

        order_args = OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
        signed = client.create_order(order_args)

        try:
            resp = client.post_order(signed, orderType=OrderType.GTC)
        except TypeError:
            resp = client.post_order(signed, OrderType.GTC)

        logger.info("Snipe order placed: token=%s price=%.2f size=%.1f resp=%s",
                     token_id[:20], price, size, resp)

        if resp and resp.get("orderID"):
            return {
                "order_id": resp["orderID"],
                "price": price,
                "size": size,
                "status": resp.get("status", "live"),
            }
        return None
    except Exception as e:
        logger.error("Snipe order error: %s", e)
        return None


def cancel_snipe_order(order_id: str):
    """Cancel a sniper order."""
    from trading import cancel_order
    cancel_order(order_id)


# ── Enabled events tracking ──────────────────────────────────────

# Store in DB which events are enabled for sniping
def get_enabled_snipe_events() -> list[str]:
    """Get list of event slugs enabled for 90¢ sniping."""
    from database import get_db
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS snipe90_events (
            slug TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            added_at INTEGER
        )""")
        conn.commit()
        rows = conn.execute("SELECT slug FROM snipe90_events WHERE enabled = 1").fetchall()
        return [r["slug"] for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def add_snipe_event(slug: str):
    from database import get_db
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS snipe90_events (
            slug TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            added_at INTEGER
        )""")
        conn.execute(
            "INSERT OR REPLACE INTO snipe90_events (slug, enabled, added_at) VALUES (?, 1, ?)",
            (slug, int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()


def remove_snipe_event(slug: str):
    from database import get_db
    conn = get_db()
    try:
        conn.execute("DELETE FROM snipe90_events WHERE slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()


def get_snipe_orders() -> list[dict]:
    """Get all active snipe orders from DB."""
    from database import get_db
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS snipe90_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_slug TEXT,
            token_id TEXT,
            condition_id TEXT,
            order_id TEXT,
            question TEXT,
            price REAL,
            size REAL,
            status TEXT DEFAULT 'LIVE',
            placed_at INTEGER
        )""")
        conn.commit()
        rows = conn.execute("SELECT * FROM snipe90_orders WHERE status = 'LIVE'").fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def save_snipe_order(event_slug: str, token_id: str, condition_id: str,
                     order_id: str, question: str, price: float, size: float):
    from database import get_db
    conn = get_db()
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS snipe90_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_slug TEXT,
            token_id TEXT,
            condition_id TEXT,
            order_id TEXT,
            question TEXT,
            price REAL,
            size REAL,
            status TEXT DEFAULT 'LIVE',
            placed_at INTEGER
        )""")
        conn.execute(
            """INSERT INTO snipe90_orders (event_slug, token_id, condition_id, order_id, question, price, size, status, placed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'LIVE', ?)""",
            (event_slug, token_id, condition_id, order_id, question, price, size, int(time.time()))
        )
        conn.commit()
    finally:
        conn.close()


def update_snipe_order_status(order_id: str, status: str):
    from database import get_db
    conn = get_db()
    try:
        conn.execute("UPDATE snipe90_orders SET status = ? WHERE order_id = ?", (status, order_id))
        conn.commit()
    finally:
        conn.close()


# ── Main sniper loop ─────────────────────────────────────────────

async def sniper90_loop(bot):
    """Main loop: check enabled events, manage orders."""
    from config import OWNER_ID, CHANNEL_ID
    from telegram.constants import ParseMode

    logger.info("Sniper 90¢ started (check every %ds)", CHECK_INTERVAL)
    await asyncio.sleep(15)  # Wait for bot to fully start

    while True:
        try:
            enabled_slugs = get_enabled_snipe_events()
            if not enabled_slugs:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            for event_slug in enabled_slugs:
                try:
                    markets = fetch_event_markets(event_slug)
                    if not markets:
                        continue

                    priced = get_market_prices(markets)
                    if not priced:
                        continue

                    # Check end date — only activate within 48h of end
                    end_str = priced[0].get("end_date", "")
                    if end_str:
                        try:
                            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                            now = datetime.now(timezone.utc)
                            hours_left = (end_dt - now).total_seconds() / 3600

                            if hours_left > HOURS_BEFORE_END:
                                logger.info("Event %s: %.0fh left (need <%dh), skipping",
                                           event_slug[:30], hours_left, HOURS_BEFORE_END)
                                continue
                            if hours_left < 0:
                                logger.info("Event %s ended, removing", event_slug[:30])
                                remove_snipe_event(event_slug)
                                continue
                        except Exception:
                            pass

                    # Top 3 by price
                    top3 = priced[:3]
                    top3_tokens = {m["token_id"] for m in top3}

                    # Get current orders for this event
                    current_orders = [o for o in get_snipe_orders() if o["event_slug"] == event_slug]
                    current_tokens = {o["token_id"] for o in current_orders}

                    # Cancel orders not in top 3 anymore
                    for order in current_orders:
                        if order["token_id"] not in top3_tokens:
                            cancel_snipe_order(order["order_id"])
                            update_snipe_order_status(order["order_id"], "CANCELLED")
                            logger.info("Cancelled snipe: %s (no longer top 3)", order["question"][:40])

                    # Place new orders for top 3 not yet placed
                    for m in top3:
                        if m["token_id"] in current_tokens:
                            continue
                        if m["yes_price"] >= 0.90:
                            # Already at 90¢+ — don't place, would fill immediately
                            logger.info("Skip %s — already at %.0f¢", m["question"][:30], m["yes_price"] * 100)
                            continue

                        result = place_snipe_order(m["token_id"], m["condition_id"])
                        if result:
                            save_snipe_order(
                                event_slug, m["token_id"], m["condition_id"],
                                result["order_id"], m["question"],
                                result["price"], result["size"],
                            )
                            logger.info("Snipe placed: %s @ 90¢", m["question"][:40])

                    # Check for filled orders
                    from trading import check_order_status
                    for order in get_snipe_orders():
                        if order["event_slug"] != event_slug:
                            continue
                        status = check_order_status(order["order_id"])
                        if status and status.lower() == "matched":
                            update_snipe_order_status(order["order_id"], "FILLED")
                            msg = (
                                f"🎯 <b>SNIPE 90¢ FILLED!</b>\n\n"
                                f"📌 <b>{order['question']}</b>\n"
                                f"💰 Bought @ 90¢ — profit 10¢/share\n"
                                f"💵 ${order['size'] * 0.90:.2f} invested\n\n"
                                f"👉 Слідкуй за ринком, постав стоп якщо потрібно"
                            )
                            try:
                                await bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode=ParseMode.HTML)
                                if CHANNEL_ID:
                                    await bot.send_message(chat_id=CHANNEL_ID, text=msg, parse_mode=ParseMode.HTML)
                            except Exception as e:
                                logger.error("Notify error: %s", e)

                    await asyncio.sleep(1)

                except Exception as e:
                    logger.error("Sniper event error %s: %s", event_slug[:20], e)

        except Exception as e:
            logger.error("Sniper90 loop error: %s", e)

        await asyncio.sleep(CHECK_INTERVAL)


# ── Get status for display ───────────────────────────────────────

def get_sniper90_status() -> str:
    """Get human-readable status."""
    enabled = get_enabled_snipe_events()
    orders = get_snipe_orders()

    lines = ["<b>🎯 Sniper 90¢ Status:</b>\n"]

    if not enabled:
        lines.append("Нема увімкнених events")
        lines.append("Додай через /snipe90")
        return "\n".join(lines)

    for slug in enabled:
        event_orders = [o for o in orders if o["event_slug"] == slug]
        lines.append(f"📅 <code>{slug}</code>")
        if event_orders:
            for o in event_orders:
                q = o["question"][:45] if o.get("question") else "?"
                lines.append(f"  • {q} — {o['status']}")
        else:
            lines.append("  • Чекає на активацію (48h до кінця)")

    return "\n".join(lines)
