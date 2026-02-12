import asyncio
import logging
import time
import hashlib
from datetime import datetime, timezone

import aiohttp
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from config import OWNER_ID, POLL_INTERVAL, CHANNEL_ID
from database import (
    get_all_traders, is_trade_seen, mark_trade_seen,
    save_buy_message, find_buy_message, find_all_open_buys, close_buy_messages,
    find_open_copy_trades, close_copy_trade, save_copy_trade,
    get_display_name, get_daily_big_trade_count, increment_daily_big_trade,
)
from polymarket_api import get_activity, detect_order_type
from trading import is_trading_enabled, place_market_sell, place_limit_buy, get_token_id_for_market
from hashtags import detect_hashtag, get_hashtag_emoji

logger = logging.getLogger(__name__)


async def _send_to_channel(bot: Bot, text: str):
    """Send copy trade notification to the dedicated channel."""
    try:
        if CHANNEL_ID:
            await bot.send_message(
                chat_id=CHANNEL_ID, text=text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
    except Exception as e:
        logger.error(f"Channel send error: {e}")


# ── Formatting helpers ───────────────────────────────────────────

def _url(trade: dict) -> str:
    es = trade.get("eventSlug", "")
    s = trade.get("slug", "")
    if es and s:
        return f"https://polymarket.com/event/{es}/{s}"
    return f"https://polymarket.com/event/{es or s}" if (es or s) else "https://polymarket.com"

def _price(p) -> str:
    try: return f"{float(p) * 100:.1f}¢"
    except: return str(p)

def _usd(v) -> str:
    try: return f"${float(v):,.2f}"
    except: return str(v)

def _shares(v) -> str:
    try: return f"{float(v):,.1f}"
    except: return str(v)

def _time(ts) -> str:
    if ts:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%H:%M UTC")
    return "?"

def _duration(secs: int) -> str:
    if secs < 60: return f"{secs}s"
    if secs < 3600: return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


# ── Message formatters ───────────────────────────────────────────

def format_buy_message(trade: dict, display_name: str, order_type: str = "❓", hashtag: str = "") -> str:
    title = trade.get("title", "Unknown Market")
    outcome = trade.get("outcome", "?")
    price = trade.get("price", 0)
    size = trade.get("size", 0)
    usdc = trade.get("usdcSize", 0)
    url = _url(trade)
    ts = trade.get("timestamp", 0)
    ht_emoji = get_hashtag_emoji(hashtag) if hashtag else ""
    ht_text = f" {ht_emoji} {hashtag}" if hashtag else ""

    return (
        f"🟢 <b>{display_name}</b> BOUGHT  {order_type}\n\n"
        f"📌 <b>{title}</b>\n"
        f"🎯 {outcome} @ {_price(price)}\n"
        f"💵 {_usd(usdc)} ({_shares(size)} shares)\n"
        f"{ht_text}\n\n"
        f"🔗 <a href=\"{url}\">Open Market</a>\n"
        f"⏰ {_time(ts)}"
    )


def format_sell_message(trade: dict, display_name: str, pnl: dict | None = None,
                        order_type: str = "❓", hashtag: str = "") -> str:
    title = trade.get("title", "Unknown Market")
    outcome = trade.get("outcome", "?")
    price = trade.get("price", 0)
    size = trade.get("size", 0)
    usdc = trade.get("usdcSize", 0)
    url = _url(trade)
    ts = trade.get("timestamp", 0)
    ht_emoji = get_hashtag_emoji(hashtag) if hashtag else ""
    ht_text = f" {ht_emoji} {hashtag}" if hashtag else ""

    lines = [
        f"🔴 <b>{display_name}</b> SOLD  {order_type}\n",
        f"📌 <b>{title}</b>",
        f"🎯 {outcome} @ {_price(price)}",
        f"💵 {_usd(usdc)} ({_shares(size)} shares)",
        f"{ht_text}" if ht_text else "",
    ]

    if pnl:
        sign = "+" if pnl["pnl_usdc"] >= 0 else ""
        emoji = "🟩" if pnl["pnl_usdc"] >= 0 else "🟥"
        lines.append("")
        lines.append(f"📊 <b>P&L:</b>")
        lines.append(f"   Entry: {_price(pnl['avg_entry'])} → Exit: {_price(pnl['sell_price'])}")
        lines.append(f"   {emoji} {sign}{_usd(pnl['pnl_usdc'])} ({sign}{pnl['pnl_pct']:.1f}%)")
        if pnl.get("hold_time"):
            lines.append(f"   ⏳ Held: {pnl['hold_time']}")

    lines.append("")
    lines.append(f"🔗 <a href=\"{url}\">Open Market</a>")
    lines.append(f"⏰ {_time(ts)}")
    return "\n".join(lines)


def format_other_message(trade: dict, display_name: str) -> str:
    tt = trade.get("type", "?")
    title = trade.get("title", "Unknown")
    usdc = trade.get("usdcSize", 0)
    url = _url(trade)
    ts = trade.get("timestamp", 0)
    emoji = {"REDEEM": "💰", "SPLIT": "✂️", "MERGE": "🔗"}.get(tt, "📊")
    return (
        f"{emoji} <b>{display_name}</b> {tt}\n"
        f"📌 <b>{title}</b>\n"
        f"💵 {_usd(usdc)}\n"
        f"🔗 <a href=\"{url}\">Open Market</a>\n"
        f"⏰ {_time(ts)}"
    )


def compute_pnl(buys: list[dict], sell_trade: dict) -> dict | None:
    if not buys:
        return None
    try:
        total_usdc = sum(float(b["usdc_size"]) for b in buys)
        total_shares = sum(float(b["size"]) for b in buys)
        if total_shares == 0:
            return None
        avg_entry = total_usdc / total_shares
        sell_price = float(sell_trade.get("price", 0))
        sell_usdc = float(sell_trade.get("usdcSize", 0))
        sell_shares = float(sell_trade.get("size", 0))
        fraction = min(sell_shares / total_shares, 1.0)
        cost = total_usdc * fraction
        pnl_usdc = sell_usdc - cost
        pnl_pct = (pnl_usdc / cost * 100) if cost > 0 else 0
        first_ts = min(int(b["timestamp"]) for b in buys)
        sell_ts = int(sell_trade.get("timestamp", time.time()))
        return {
            "avg_entry": avg_entry, "sell_price": sell_price,
            "pnl_usdc": pnl_usdc, "pnl_pct": pnl_pct,
            "total_invested": total_usdc,
            "hold_time": _duration(sell_ts - first_ts),
        }
    except Exception as e:
        logger.error(f"PnL error: {e}")
        return None


# ── Autocopy amount calculator ──────────────────────────────────

def calc_autocopy_amount(trader_usdc: float, trader_address: str, price: float = 0) -> float | None:
    """
    Calculate how much to spend based on trader's trade size.
    < $1  → copy exact amount
    $2-$10 → $1
    $10-$20 → $2
    $20-$50 → $3
    $50+ → $5 (max 1x per day)
    If 5-share minimum costs more than calculated amount, use 5*price instead.
    Returns None if should skip.
    """
    if trader_usdc < 1.0:
        amount = trader_usdc
    elif trader_usdc < 2.0:
        amount = 1.0
    elif trader_usdc < 10.0:
        amount = 1.0
    elif trader_usdc < 20.0:
        amount = 2.0
    elif trader_usdc < 50.0:
        amount = 3.0
    else:
        count = get_daily_big_trade_count(trader_address)
        if count >= 1:
            return None
        amount = 5.0

    # Ensure minimum 5 shares
    if price > 0:
        min_cost = 5.0 * price  # 5 shares minimum
        if amount < min_cost:
            amount = min_cost

    return amount


# ── Copy trade button builder ────────────────────────────────────

pending_copy_data: dict[str, dict] = {}


def _clean_pending_data():
    """Remove entries older than 1 hour."""
    now = time.time()
    expired = [k for k, v in pending_copy_data.items() if now - v.get("_ts", 0) > 3600]
    for k in expired:
        del pending_copy_data[k]


# ── Main poller ──────────────────────────────────────────────────

async def poll_traders(bot: Bot):
    logger.info("Poller started (interval=%ds)", POLL_INTERVAL)

    while True:
        try:
            _clean_pending_data()  # Remove expired copy-trade buttons
            traders = get_all_traders()
            if traders:
                async with aiohttp.ClientSession() as session:
                    for trader in traders:
                        address = trader["address"]
                        display_name = get_display_name(trader)
                        is_autocopy = trader.get("autocopy", 0) == 1
                        try:
                            activities = await get_activity(session, address, limit=30)
                            new_trades = []
                            for act in activities:
                                tx = act.get("transactionHash", "")
                                if not tx:
                                    continue
                                if not is_trade_seen(address, tx):
                                    new_trades.append(act)
                                    mark_trade_seen(address, tx, int(act.get("timestamp", time.time())))

                            for trade in sorted(new_trades, key=lambda x: int(x.get("timestamp", 0))):
                                await _send_notification(bot, trade, address, display_name, is_autocopy)

                        except Exception as e:
                            logger.error(f"Poll error {address}: {e}")
                        await asyncio.sleep(0.5)

            # Report success to health monitor
            try:
                from health import report_poll_success
                report_poll_success()
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Poller error: {e}")
            try:
                from health import report_poll_error
                report_poll_error()
            except Exception:
                pass

        await asyncio.sleep(POLL_INTERVAL)


async def _send_notification(bot: Bot, trade: dict, address: str, display_name: str, is_autocopy: bool):
    trade_type = trade.get("type", "TRADE")
    side = trade.get("side", "")
    condition_id = trade.get("conditionId", "")
    outcome = trade.get("outcome", "?")
    token_id = trade.get("asset", "")
    tx_hash = trade.get("transactionHash", "")
    title = trade.get("title", "")

    # Detect hashtag
    hashtag = detect_hashtag(title)

    # Detect order type (Limit vs Market)
    order_type = "❓"
    if trade_type == "TRADE" and tx_hash:
        try:
            async with aiohttp.ClientSession() as det_session:
                order_type = await detect_order_type(det_session, tx_hash, address)
        except Exception:
            order_type = "❓"

    if trade_type == "TRADE" and side == "BUY":
        msg_text = format_buy_message(trade, display_name, order_type, hashtag)

        # Build copy trade button
        keyboard = None
        if is_trading_enabled():
            trade_hash = hashlib.md5(
                f"{condition_id}{outcome}{trade.get('price', 0)}{token_id}".encode()
            ).hexdigest()[:12]
            pending_copy_data[trade_hash] = {
                "condition_id": condition_id,
                "outcome": outcome,
                "price": float(trade.get("price", 0)),
                "token_id": token_id,
                "title": title,
                "trader_address": address,
                "trader_name": display_name,
                "slug": trade.get("slug", ""),
                "event_slug": trade.get("eventSlug", ""),
                "hashtag": hashtag,
                "_ts": time.time(),
            }
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("💰 Copy Trade", callback_data=f"ct:{trade_hash}"),
            ]])

        sent = await bot.send_message(
            chat_id=OWNER_ID, text=msg_text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            reply_markup=keyboard,
        )

        # Save BUY for future SELL reply
        try:
            save_buy_message(
                trader_address=address, condition_id=condition_id,
                outcome=outcome, buy_price=float(trade.get("price", 0)),
                usdc_size=float(trade.get("usdcSize", 0)),
                size=float(trade.get("size", 0)),
                message_id=sent.message_id,
                timestamp=int(trade.get("timestamp", time.time())),
                title=title, token_id=token_id, hashtag=hashtag,
            )
        except Exception as e:
            logger.error(f"Save buy error: {e}")

        # ── AUTOCOPY ──
        if is_autocopy and is_trading_enabled():
            await _handle_autocopy_buy(bot, trade, address, display_name, hashtag, order_type)

    elif trade_type == "TRADE" and side == "SELL":
        buys = find_all_open_buys(address, condition_id, outcome)
        buy_msg = find_buy_message(address, condition_id, outcome)
        pnl = compute_pnl(buys, trade) if buys else None

        # Get hashtag from buy record
        if buy_msg and buy_msg.get("hashtag"):
            hashtag = buy_msg["hashtag"]

        msg_text = format_sell_message(trade, display_name, pnl, order_type, hashtag)
        reply_to = buy_msg["message_id"] if buy_msg else None

        try:
            await bot.send_message(
                chat_id=OWNER_ID, text=msg_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_to_message_id=reply_to,
            )
        except Exception:
            await bot.send_message(
                chat_id=OWNER_ID, text=msg_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )

        # Close with P&L data
        if buys:
            sell_price = float(trade.get("price", 0))
            sell_usdc = float(trade.get("usdcSize", 0))
            pnl_usdc = pnl["pnl_usdc"] if pnl else 0
            pnl_pct = pnl["pnl_pct"] if pnl else 0
            close_buy_messages(address, condition_id, outcome,
                             sell_price=sell_price, sell_usdc=sell_usdc,
                             pnl_usdc=pnl_usdc, pnl_pct=pnl_pct)

        # Auto-sell copy trades
        await _auto_sell_copies(bot, address, condition_id, outcome, trade)

    elif trade_type == "REDEEM":
        buys = find_all_open_buys(address, condition_id, outcome)
        buy_msg = find_buy_message(address, condition_id, outcome)

        if buy_msg and buy_msg.get("hashtag"):
            hashtag = buy_msg["hashtag"]

        pnl_lines = ""
        pnl_usdc = 0
        pnl_pct = 0
        if buys:
            try:
                total_in = sum(float(b["usdc_size"]) for b in buys)
                redeemed = float(trade.get("usdcSize", 0))
                pnl_usdc = redeemed - total_in
                pnl_pct = (pnl_usdc / total_in * 100) if total_in > 0 else 0
                sign = "+" if pnl_usdc >= 0 else ""
                emoji = "🟩" if pnl_usdc >= 0 else "🟥"
                avg = total_in / sum(float(b["size"]) for b in buys)
                first_ts = min(int(b["timestamp"]) for b in buys)
                hold = _duration(int(trade.get("timestamp", time.time())) - first_ts)
                pnl_lines = (
                    f"\n📊 <b>P&L:</b>\n"
                    f"   Entry: {_price(avg)} → Resolved\n"
                    f"   {emoji} {sign}{_usd(pnl_usdc)} ({sign}{pnl_pct:.1f}%)\n"
                    f"   ⏳ Held: {hold}"
                )
            except Exception:
                pass

        ht_text = f" {get_hashtag_emoji(hashtag)} {hashtag}" if hashtag else ""
        msg_text = (
            f"💰 <b>{display_name}</b> REDEEMED\n\n"
            f"📌 <b>{trade.get('title', '?')}</b>\n"
            f"💵 {_usd(trade.get('usdcSize', 0))}"
            f"{ht_text}"
            f"{pnl_lines}\n\n"
            f"🔗 <a href=\"{_url(trade)}\">Open Market</a>\n"
            f"⏰ {_time(trade.get('timestamp', 0))}"
        )

        reply_to = buy_msg["message_id"] if buy_msg else None
        try:
            await bot.send_message(
                chat_id=OWNER_ID, text=msg_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                reply_to_message_id=reply_to,
            )
        except Exception:
            await bot.send_message(
                chat_id=OWNER_ID, text=msg_text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )

        if buys:
            sell_usdc = float(trade.get("usdcSize", 0))
            close_buy_messages(address, condition_id, outcome,
                             sell_price=1.0, sell_usdc=sell_usdc,
                             pnl_usdc=pnl_usdc, pnl_pct=pnl_pct)
        await _auto_sell_copies(bot, address, condition_id, outcome, trade)

    else:
        msg_text = format_other_message(trade, display_name)
        await bot.send_message(
            chat_id=OWNER_ID, text=msg_text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )


# ── Autocopy BUY handler ────────────────────────────────────────

async def _handle_autocopy_buy(bot: Bot, trade: dict, trader_address: str, trader_name: str, hashtag: str, order_type: str = "❓"):
    """Automatically copy a BUY trade based on size rules."""
    from database import get_autocopy_tags

    # Check if hashtag is allowed for this trader's autocopy
    allowed_tags = get_autocopy_tags(trader_address)
    if allowed_tags and hashtag not in allowed_tags:
        logger.info("Autocopy skip: %s not in allowed tags %s for %s", hashtag, allowed_tags, trader_name)
        return
    trader_usdc = float(trade.get("usdcSize", 0))
    price = float(trade.get("price", 0))
    condition_id = trade.get("conditionId", "")
    outcome = trade.get("outcome", "?")
    token_id = trade.get("asset", "")
    title = trade.get("title", "")

    # Determine if trader used a limit order
    is_limit = "📋" in order_type  # 📋 = Limit, 📊 = Market

    amount = calc_autocopy_amount(trader_usdc, trader_address, price)
    if amount is None:
        logger.info("Autocopy skip: $50+ limit reached for %s today", trader_name)
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"⏭ <b>Autocopy skipped</b> — $50+ daily limit reached for {trader_name}",
            parse_mode=ParseMode.HTML,
        )
        return

    if amount < 0.01:
        return

    # For limit orders: don't enforce $1 minimum (postOnly has no minimum)
    if is_limit and amount < 1.0:
        # Keep the small amount — postOnly allows it
        pass

    # Resolve token_id
    if not token_id:
        token_id = get_token_id_for_market(condition_id, outcome) or ""
    if not token_id:
        logger.error("Autocopy: no token_id for %s", title)
        return

    # Limit → postOnly (sits in book), Market → GTC (executes now)
    result = place_limit_buy(token_id, price, amount, condition_id, post_only=is_limit)
    order_label = "📋 Limit" if is_limit else "📊 Market"

    if result:
        shares = result["size"]
        order_id = result.get("order_id", "")

        # For postOnly orders, verify it actually went live (not rejected)
        if is_limit and order_id:
            await asyncio.sleep(2)  # Wait 2s for order to settle
            from trading import check_order_status
            status = check_order_status(order_id)
            if status and status not in ("live", "matched", "filled"):
                logger.warning("Autocopy postOnly order %s rejected (status: %s)", order_id, status)
                await bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"⏭ <b>Autocopy skipped</b> — лімітка відхилена\n"
                        f"📌 {title[:50]}\n"
                        f"Status: {status}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                return

        # Track $50+ trades
        if trader_usdc >= 50:
            increment_daily_big_trade(trader_address)

        save_copy_trade(
            trader_address=trader_address,
            condition_id=condition_id,
            token_id=token_id,
            outcome=outcome,
            buy_price=price,
            usdc_spent=amount,
            shares=shares,
            order_id=order_id,
            timestamp=int(time.time()),
            title=title,
            hashtag=hashtag,
            source="autocopy",
        )

        await bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🤖 <b>AUTOCOPY</b> — copying {trader_name}\n\n"
                f"📌 <b>{title}</b>\n"
                f"🎯 BUY {outcome} @ {_price(price)} ({order_label})\n"
                f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
                f"👤 Trader put: {_usd(trader_usdc)}\n\n"
                f"🤖 Will auto-sell when {trader_name} exits."
            ),
            parse_mode=ParseMode.HTML,
        )
        # Forward to channel
        await _send_to_channel(bot,
            f"🟢 <b>AUTOCOPY BUY</b> ({order_label})\n\n"
            f"📌 <b>{title}</b>\n"
            f"🎯 {outcome} @ {_price(price)}\n"
            f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
            f"👤 Copying: {trader_name} ({_usd(trader_usdc)})"
        )
    else:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚠️ <b>Autocopy FAILED</b> for {title}\nCheck balance/allowances.",
            parse_mode=ParseMode.HTML,
        )


# ── Auto-sell copy trades ────────────────────────────────────────

async def _auto_sell_copies(bot: Bot, trader_address: str, condition_id: str, outcome: str, sell_trade: dict):
    copies = find_open_copy_trades(trader_address, condition_id, outcome)
    if not copies:
        return

    # Get trader display name
    traders = get_all_traders()
    trader_name = "?"
    for t in traders:
        if t["address"] == trader_address:
            trader_name = get_display_name(t)
            break

    sell_price = float(sell_trade.get("price", 0))
    sell_ts = int(sell_trade.get("timestamp", time.time()))

    for copy in copies:
        try:
            token_id = copy["token_id"]
            shares = float(copy["shares"])
            invested = float(copy["usdc_spent"])

            result = place_market_sell(token_id, shares, condition_id)

            if result:
                sell_usdc = shares * sell_price
                pnl_usdc = sell_usdc - invested
                pnl_pct = (pnl_usdc / invested * 100) if invested > 0 else 0

                close_copy_trade(copy["id"], sell_price, sell_usdc, sell_ts,
                               pnl_usdc=pnl_usdc, pnl_pct=pnl_pct)

                sign = "+" if pnl_usdc >= 0 else ""
                emoji = "🟩" if pnl_usdc >= 0 else "🟥"
                hold = _duration(sell_ts - int(copy["timestamp"]))
                source = "AUTOCOPY" if copy.get("source") == "autocopy" else "COPY"

                msg = (
                    f"🤖 <b>AUTO-SOLD</b> ({source})\n\n"
                    f"📌 <b>{copy.get('title', '?')}</b>\n"
                    f"🎯 {outcome} @ {_price(sell_price)}\n"
                    f"💵 {_usd(sell_usdc)} ({_shares(shares)} shares)\n"
                    f"👤 Trader: {trader_name}\n\n"
                    f"📊 <b>Your P&L:</b>\n"
                    f"   Entry: {_price(copy['buy_price'])} → Exit: {_price(sell_price)}\n"
                    f"   {emoji} {sign}{_usd(pnl_usdc)} ({sign}{pnl_pct:.1f}%)\n"
                    f"   ⏳ Held: {hold}"
                )
                await bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode=ParseMode.HTML)
                # Forward to channel
                await _send_to_channel(bot, msg)
            else:
                # Sell failed — likely ghost trade (never filled or already sold)
                # Close it in DB so it doesn't spam again
                close_copy_trade(copy["id"], sell_price, 0, sell_ts,
                               pnl_usdc=-invested, pnl_pct=-100)
                logger.warning(f"Auto-sell failed for {copy.get('title', '?')}, closing ghost trade in DB")
                await bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"⚠️ <b>Auto-sell FAILED</b>\n"
                        f"📌 {copy.get('title', '?')[:50]}\n"
                        f"💵 {_usd(invested)} ({_shares(shares)} shares)\n"
                        f"❌ Закрив запис в БД (шейрів скоріш за все нема)\n"
                        f"👉 Перевір позицію на polymarket.com"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.error(f"Auto-sell error: {e}")
