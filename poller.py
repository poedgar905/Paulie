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
    < $1   → copy exact amount (as-is)
    $1-$2  → copy exact amount (as-is)
    $2-$10 → $2
    $10-$50 → $3
    $50+   → $5 (max bet)
    Returns None if should skip.
    """
    if trader_usdc < 1.0:
        amount = trader_usdc
    elif trader_usdc <= 2.0:
        amount = trader_usdc
    elif trader_usdc <= 10.0:
        amount = 2.0
    elif trader_usdc <= 50.0:
        amount = 3.0
    else:
        amount = 5.0

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
                                cid = act.get("conditionId", "")
                                side = act.get("side", "")
                                if not is_trade_seen(address, tx, cid, side):
                                    new_trades.append(act)
                                    mark_trade_seen(address, tx, int(act.get("timestamp", time.time())), cid, side)

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
            await _handle_autocopy_buy(bot, trade, address, display_name, hashtag)

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

        # Auto-sell copy trades (OPEN ones)
        await _auto_sell_copies(bot, address, condition_id, outcome, trade)

        # Cancel any PENDING orders for this market (trader already exited)
        await _cancel_pending_copies(bot, address, condition_id, outcome)

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
        await _cancel_pending_copies(bot, address, condition_id, outcome)

    else:
        msg_text = format_other_message(trade, display_name)
        await bot.send_message(
            chat_id=OWNER_ID, text=msg_text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )


# ── Autocopy BUY handler ────────────────────────────────────────

async def _handle_autocopy_buy(bot: Bot, trade: dict, trader_address: str, trader_name: str, hashtag: str):
    """Automatically copy a BUY trade — place GTC at trader's price and save."""
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

    # Resolve token_id
    if not token_id:
        token_id = get_token_id_for_market(condition_id, outcome) or ""
    if not token_id:
        logger.error("Autocopy: no token_id for %s", title)
        return

    result = place_limit_buy(token_id, price, amount, condition_id)

    if result:
        shares = result["size"]
        order_id = result.get("order_id", "")

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
            status="PENDING",
        )

        await bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🤖 <b>AUTOCOPY</b> — copying {trader_name}\n\n"
                f"📌 <b>{title}</b>\n"
                f"🎯 BUY {outcome} @ {_price(price)}\n"
                f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
                f"👤 Trader put: {_usd(trader_usdc)}\n\n"
                f"🤖 Will auto-sell when {trader_name} exits."
            ),
            parse_mode=ParseMode.HTML,
        )
        # Forward to channel
        await _send_to_channel(bot,
            f"🟢 <b>AUTOCOPY BUY</b>\n\n"
            f"📌 <b>{title}</b>\n"
            f"🎯 {outcome} @ {_price(price)}\n"
            f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
            f"👤 Copying: {trader_name} ({_usd(trader_usdc)})"
        )
    else:
        # Get diagnostic info
        from trading import get_balance, debug_balance_info
        bal = get_balance()
        diag = ""
        if token_id:
            diag = debug_balance_info(token_id)
        await bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"⚠️ <b>Autocopy FAILED</b>\n"
                f"📌 {title}\n"
                f"🎯 {outcome} @ {_price(price)} | ${amount:.2f}\n"
                f"💰 Balance: ${bal:.2f if bal else '?'}\n"
                f"🔧 {diag[:200] if diag else 'no diag'}"
            ),
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
    trader_sell_shares = float(sell_trade.get("size", 0))

    # Calculate what fraction of his position the trader is selling
    trader_buys = find_all_open_buys(trader_address, condition_id, outcome)
    trader_total_shares = sum(float(b.get("size", 0)) for b in trader_buys) if trader_buys else 0

    if trader_total_shares <= 0 or trader_sell_shares >= trader_total_shares * 0.95:
        sell_fraction = 1.0
    else:
        sell_fraction = min(trader_sell_shares / trader_total_shares, 1.0)

    logger.info("Autocopy sell: trader sells %.1f/%.1f (%.0f%%)",
                trader_sell_shares, trader_total_shares, sell_fraction * 100)

    for copy in copies:
        try:
            token_id = copy["token_id"]
            total_shares = float(copy["shares"])
            invested = float(copy["usdc_spent"])

            shares_to_sell = round(total_shares * sell_fraction, 2)
            if shares_to_sell < 0.1:
                shares_to_sell = total_shares

            result = place_market_sell(token_id, shares_to_sell, condition_id)

            if result:
                sell_usdc = shares_to_sell * sell_price
                cost_fraction = invested * sell_fraction
                pnl_usdc = sell_usdc - cost_fraction
                pnl_pct = (pnl_usdc / cost_fraction * 100) if cost_fraction > 0 else 0

                if sell_fraction >= 0.95:
                    close_copy_trade(copy["id"], sell_price, sell_usdc, sell_ts,
                                   pnl_usdc=pnl_usdc, pnl_pct=pnl_pct)
                    action = "SOLD ALL"
                else:
                    remaining_shares = round(total_shares - shares_to_sell, 2)
                    remaining_cost = round(invested - cost_fraction, 2)
                    _update_copy_partial_sell(copy["id"], remaining_shares, remaining_cost)
                    action = f"SOLD {sell_fraction*100:.0f}%"

                sign = "+" if pnl_usdc >= 0 else ""
                emoji = "🟩" if pnl_usdc >= 0 else "🟥"
                hold = _duration(sell_ts - int(copy["timestamp"]))

                msg = (
                    f"🤖 <b>AUTO-{action}</b>\n\n"
                    f"📌 <b>{copy.get('title', '?')}</b>\n"
                    f"🎯 {outcome} @ {_price(sell_price)}\n"
                    f"💵 {_usd(sell_usdc)} ({_shares(shares_to_sell)} sh)\n"
                    f"👤 Trader: {trader_name} sold {_shares(trader_sell_shares)}/{_shares(trader_total_shares)}\n\n"
                    f"📊 <b>P&L:</b>\n"
                    f"   {_price(copy['buy_price'])} → {_price(sell_price)}\n"
                    f"   {emoji} {sign}{_usd(pnl_usdc)} ({sign}{pnl_pct:.1f}%)\n"
                    f"   ⏳ {hold}"
                )
                await bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode=ParseMode.HTML)
                await _send_to_channel(bot, msg)
            else:
                logger.warning(f"Auto-sell failed for {copy.get('title', '?')}")
                await bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"⚠️ <b>Auto-sell FAILED</b>\n"
                        f"📌 {copy.get('title', '?')[:50]}\n"
                        f"👉 Перевір позицію на polymarket.com"
                    ),
                    parse_mode=ParseMode.HTML,
                )
        except Exception as e:
            logger.error(f"Auto-sell error: {e}")


def _update_copy_partial_sell(copy_id: int, remaining_shares: float, remaining_cost: float):
    """Update copy trade after partial sell — keep it OPEN with reduced size."""
    from config import DB_PATH
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE copy_trades SET shares = ?, usdc_spent = ? WHERE id = ?",
        (remaining_shares, remaining_cost, copy_id)
    )
    conn.commit()
    conn.close()


# ── Cancel PENDING orders when trader exits ──────────────────────

async def _cancel_pending_copies(bot: Bot, trader_address: str, condition_id: str, outcome: str):
    """Cancel PENDING copy trades when trader sells — no point keeping limit order."""
    from database import find_pending_copy_trades, update_copy_trade_status
    from trading import cancel_order

    pending = find_pending_copy_trades(trader_address, condition_id, outcome)
    for p in pending:
        order_id = p.get("order_id", "")
        if order_id:
            cancel_order(order_id)
        update_copy_trade_status(p["id"], "CANCELLED")
        logger.info("Cancelled PENDING copy trade %s (trader exited)", p.get("title", "?")[:40])


# ── Background order checker ─────────────────────────────────────

async def check_pending_orders(bot: Bot):
    """Background task: check PENDING orders every 30s.
    - MATCHED → OPEN (+ check if trader already sold → auto-sell)
    - LIVE > 2 min → cancel + CANCELLED
    """
    from database import (
        get_all_pending_copy_trades, update_copy_trade_status,
        has_trader_sold, get_all_traders,
    )
    from trading import check_order_status, cancel_order

    logger.info("Order checker started (30s interval)")
    await asyncio.sleep(30)  # Wait before first check

    while True:
        try:
            pending = get_all_pending_copy_trades()
            traders = {t["address"]: get_display_name(t) for t in get_all_traders()}

            for p in pending:
                order_id = p.get("order_id", "")
                if not order_id:
                    update_copy_trade_status(p["id"], "CANCELLED")
                    continue

                status = check_order_status(order_id)
                status_lower = status.lower() if status else ""
                age = time.time() - int(p.get("timestamp", time.time()))

                if status_lower == "matched":
                    # Order filled! Move to OPEN
                    update_copy_trade_status(p["id"], "OPEN")
                    trader_name = traders.get(p["trader_address"], "?")
                    logger.info("PENDING → OPEN: %s (%s)", p.get("title", "?")[:40], trader_name)

                    await bot.send_message(
                        chat_id=OWNER_ID,
                        text=(
                            f"✅ <b>Ордер заповнився!</b>\n"
                            f"📌 {p.get('title', '?')[:50]}\n"
                            f"🎯 {p['outcome']} @ {_price(p['buy_price'])}\n"
                            f"💵 {_usd(p['usdc_spent'])} ({_shares(p['shares'])} shares)"
                        ),
                        parse_mode=ParseMode.HTML,
                    )

                    # Check if trader already sold this market
                    if has_trader_sold(p["trader_address"], p["condition_id"], p["outcome"]):
                        logger.info("Trader already sold — auto-selling filled order")
                        result = place_market_sell(p["token_id"], float(p["shares"]), p["condition_id"])
                        if result:
                            close_copy_trade(p["id"], 0, 0, int(time.time()), pnl_usdc=0, pnl_pct=0)
                            await bot.send_message(
                                chat_id=OWNER_ID,
                                text=(
                                    f"🤖 <b>AUTO-SOLD</b> (трейдер вже вийшов)\n"
                                    f"📌 {p.get('title', '?')[:50]}"
                                ),
                                parse_mode=ParseMode.HTML,
                            )

                elif status_lower == "live" and age > 120:
                    # Still live after 2 min → cancel
                    cancel_order(order_id)
                    update_copy_trade_status(p["id"], "CANCELLED")
                    logger.info("PENDING → CANCELLED (timeout): %s", p.get("title", "?")[:40])

                elif status_lower not in ("live", "matched", ""):
                    # Unexpected status (cancelled externally, expired, etc)
                    update_copy_trade_status(p["id"], "CANCELLED")
                    logger.info("PENDING → CANCELLED (status %s): %s", status, p.get("title", "?")[:40])

                await asyncio.sleep(0.3)  # Don't hammer API

        except Exception as e:
            logger.error(f"Order checker error: {e}")

        await asyncio.sleep(30)
