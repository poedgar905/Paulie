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
    find_open_copy_trades, find_open_copy_trades_by_token, close_copy_trade, save_copy_trade,
    get_display_name,
)
from polymarket_api import get_activity, detect_order_type
from trading import is_trading_enabled, place_market_sell, place_fok_buy, smart_sell, get_token_id_for_market
from hashtags import detect_hashtag, get_hashtag_emoji

logger = logging.getLogger(__name__)


async def _send_to_channel(bot: Bot, text: str):
    """Send copy trade notification to the dedicated channel."""
    if not CHANNEL_ID:
        return
    try:
        await bot.send_message(
            chat_id=CHANNEL_ID, text=text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Channel send error: {e}")
        try:
            import re
            clean = re.sub(r'<[^>]*>', '', text)
            await bot.send_message(chat_id=CHANNEL_ID, text=clean, disable_web_page_preview=True)
        except Exception:
            pass


async def _safe_send(bot: Bot, chat_id, text: str, **kwargs):
    """Send HTML message, auto-escape if Telegram parse fails."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, **kwargs)
    except Exception as e:
        err = str(e).lower()
        if "parse entities" in err or "unsupported start tag" in err:
            import re
            # Escape all < that aren't valid HTML tags
            clean = re.sub(r'<(?!/?(?:b|i|a|code|pre|s|u)\b)', '&lt;', text)
            try:
                return await bot.send_message(chat_id=chat_id, text=clean, parse_mode=ParseMode.HTML, **kwargs)
            except Exception:
                clean2 = re.sub(r'<[^>]*>', '', text)
                return await bot.send_message(chat_id=chat_id, text=clean2, **kwargs)
        raise


# ── Formatting helpers ───────────────────────────────────────────

def _url(trade: dict) -> str:
    es = trade.get("eventSlug", "")
    s = trade.get("slug", "")
    if es and s:
        return f"https://polymarket.com/event/{es}/{s}"
    return f"https://polymarket.com/event/{es or s}" if (es or s) else "https://polymarket.com"

def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
    title = _esc(trade.get("title", "Unknown Market"))
    outcome = _esc(trade.get("outcome", "?"))
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
    title = _esc(trade.get("title", "Unknown Market"))
    outcome = _esc(trade.get("outcome", "?"))
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
    Proportional copy: COPY_RATIO × trader amount.
    Ensures we maintain same proportions across all ranges.
    """
    from risk_manager import calc_copy_amount, can_afford, adjust_amount_to_budget

    amount = calc_copy_amount(trader_usdc)

    ok, available, exposure = can_afford(amount)
    if not ok and available > 0:
        # Not enough for full amount — reduce but don't skip
        amount = adjust_amount_to_budget(amount, available)
    elif not ok:
        # Truly no cash
        return None

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
    title_safe = _esc(title)

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
    title = _esc(trade.get("title", ""))

    amount = calc_autocopy_amount(trader_usdc, trader_address, price)
    if amount is None:
        from trading import get_balance
        from database import get_total_open_exposure
        bal = get_balance() or 0
        exp = get_total_open_exposure()
        logger.info("Autocopy skip: no cash (bal=$%.2f, exp=$%.2f) for %s", bal, exp, trader_name)
        await bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"⏭ <b>Autocopy skip</b> — мало балансу\n"
                f"💰 Cash: ${bal:.2f} | Open: ${exp:.2f}\n"
                f"📌 {title}\n"
                f"👉 Закинь USDC на гаманець"
            ),
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

    # Check available balance (on-chain USDC minus pending order costs)
    from trading import get_balance
    from database import get_all_pending_copy_trades, get_all_open_copy_trades
    bal = get_balance()
    if bal is not None:
        # Subtract cost of all PENDING (live) orders from available balance
        pending = get_all_pending_copy_trades()
        pending_cost = sum(float(p.get("usdc_spent", 0)) for p in pending)
        available = bal - pending_cost
        logger.info("Balance check: on-chain=$%.2f, pending_orders=$%.2f, available=$%.2f, need=$%.2f",
                     bal, pending_cost, available, amount)

        if available < amount:
            logger.warning("Not enough available balance ($%.2f < $%.2f)", available, amount)
            await bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"⏭ <b>Autocopy skip</b> — мало балансу\n"
                    f"💰 Cash: ${bal:.2f} | Pending: ${pending_cost:.2f} | Free: ${available:.2f}\n"
                    f"📌 {title}"
                ),
                parse_mode=ParseMode.HTML,
            )
            return

    result = place_fok_buy(token_id, price, amount, condition_id)

    if result:
        shares = result["size"]
        order_id = result.get("order_id", "")

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
            status=result.get("status", "PENDING"),
        )

        fill_status = result.get("status", "PENDING")
        if fill_status == "FILLED":
            status_emoji = "✅"
            status_text = "FILLED одразу"
            # Post to channel immediately
            await _send_to_channel(bot,
                f"🟢 <b>AUTOCOPY BUY</b>\n\n"
                f"📌 <b>{title}</b>\n"
                f"🎯 {outcome} @ {_price(result['price'])}\n"
                f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
                f"👤 Copying: {trader_name} ({_usd(trader_usdc)})"
            )
        else:
            status_emoji = "⏳"
            status_text = "PENDING"

        await bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🤖 <b>AUTOCOPY</b> — copying {trader_name}\n\n"
                f"📌 <b>{title}</b>\n"
                f"🎯 BUY {outcome} @ {_price(result['price'])}\n"
                f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
                f"👤 Trader put: {_usd(trader_usdc)}\n"
                f"{status_emoji} {status_text}"
            ),
            parse_mode=ParseMode.HTML,
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
                f"💰 Balance: ${bal:.2f}\n" if bal else f"💰 Balance: ?\n"
                f"🔧 {diag[:200] if diag else 'no diag'}"
            ),
            parse_mode=ParseMode.HTML,
        )


# ── Auto-sell copy trades ────────────────────────────────────────

async def _auto_sell_copies(bot: Bot, trader_address: str, condition_id: str, outcome: str, sell_trade: dict):
    """Auto-sell our copies when trader sells. Match by token_id to avoid cross-market confusion."""
    token_id = sell_trade.get("asset", "")
    if not token_id:
        # Fallback to condition_id matching
        copies = find_open_copy_trades(trader_address, condition_id, outcome)
    else:
        # Match by exact token_id — prevents selling wrong market
        copies = find_open_copy_trades_by_token(trader_address, token_id)

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

            result = smart_sell(token_id, shares_to_sell, sell_price, condition_id)

            if result and result.get("status") == "ghost":
                # No shares on-chain — close ghost trade
                close_copy_trade(copy["id"], 0, 0, sell_ts, pnl_usdc=-invested, pnl_pct=-100)
                logger.warning("Ghost trade closed: %s", _esc(copy.get("title", "?")))
                continue

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
                    f"📌 <b>{_esc(copy.get('title', '?'))}</b>\n"
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
                logger.warning(f"Auto-sell failed for {_esc(copy.get('title', '?'))}")
                await bot.send_message(
                    chat_id=OWNER_ID,
                    text=(
                        f"⚠️ <b>Auto-sell FAILED</b>\n"
                        f"📌 {_esc(copy.get('title', '?'))[:50]}\n"
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
        has_trader_sold, has_trader_sold_token, get_all_traders,
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
                            f"📌 {_esc(p.get('title', '?'))[:50]}\n"
                            f"🎯 {p['outcome']} @ {_price(p['buy_price'])}\n"
                            f"💵 {_usd(p['usdc_spent'])} ({_shares(p['shares'])} shares)"
                        ),
                        parse_mode=ParseMode.HTML,
                    )

                    # NOW post to channel — only after confirmed fill
                    await _send_to_channel(bot,
                        f"🟢 <b>AUTOCOPY BUY</b>\n\n"
                        f"📌 <b>{_esc(p.get('title', '?'))}</b>\n"
                        f"🎯 {p['outcome']} @ {_price(p['buy_price'])}\n"
                        f"💵 {_usd(p['usdc_spent'])} ({_shares(p['shares'])} shares)\n"
                        f"👤 Copying: {trader_name}"
                    )

                    # Don't auto-sell here. Let normal polling detect trader's SELL
                    # and handle it proportionally via _auto_sell_copies.
                    logger.info("Order filled, waiting for trader sell signal via poll")

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
