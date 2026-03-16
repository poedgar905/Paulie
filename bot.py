import asyncio
import logging
import re
import time
from functools import wraps

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from config import BOT_TOKEN, OWNER_ID, FUNDER_ADDRESS
from database import (
    init_db, add_trader, remove_trader, get_all_traders, update_trader,
    seed_existing_trades, save_copy_trade, get_display_name,
    set_nickname, set_autocopy, set_autocopy_tags, find_trader_by_name,
    get_all_open_copy_trades, close_copy_trade, update_copy_trade_status,
    get_all_pending_copy_trades,
)
from polymarket_api import (
    extract_address_or_username, resolve_username_to_address,
    get_profile, get_activity,
)
from poller import (
    poll_traders, format_buy_message, format_sell_message,
    format_other_message, pending_copy_data,
)
from trading import (
    is_trading_enabled, get_balance, place_fok_buy,
    get_token_id_for_market,
)
from hashtags import detect_hashtag

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────
def _price(p) -> str:
    try: return f"{float(p) * 100:.1f}¢"
    except: return str(p)

def _usd(v) -> str:
    try: return f"${float(v):,.2f}"
    except: return str(v)

def _shares(v) -> str:
    try: return f"{float(v):,.1f}"
    except: return str(v)


# ── Auth ────────────────────────────────────────────────────────
def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else None
        if uid != OWNER_ID:
            if update.message:
                await update.message.reply_text("⛔ Access denied.")
            elif update.callback_query:
                await update.callback_query.answer("⛔", show_alert=True)
            return
        return await func(update, context)
    return wrapper


# ── /start ──────────────────────────────────────────────────────
@owner_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trading_status = "✅ Enabled" if is_trading_enabled() else "❌ Disabled (set PRIVATE_KEY)"
    await update.message.reply_text(
        f"👋 <b>Polymarket Tracker Bot</b>\n\n"
        f"📋 <b>Commands:</b>\n"
        f"/add <code>@username</code> — Track trader\n"
        f"/remove <code>name</code> — Stop tracking\n"
        f"/nick <code>name NewNick</code> — Set nickname\n"
        f"/list — Watchlist\n"
        f"/check — Latest trades now\n"
        f"/balance — Баланс і P&L\n"
        f"/portfolio — Your open copy-trades\n"
        f"/autocopy <code>name ON/OFF</code> — Auto copy-trading\n\n"
        f"🎯 <b>Sniper:</b>\n"
        f"/snipe <code>event_url</code> — Ручний снайпер\n"
        f"/snipe_auto — 🤖 Авто-снайпер (Binance тригер)\n"
        f"/snipe_status — Статус\n"
        f"/snipe_stop — Зупинити всіх\n\n"
        f"🔄 Polls every 3 sec\n"
        f"📊 Google Sheets updates every 5 min\n"
        f"🟢 BUY → with [Copy Trade] button\n"
        f"🔴 SELL → reply to BUY + P&L\n"
        f"🤖 Auto-sell when trader exits\n\n"
        f"💰 Trading: {trading_status}\n"
        f"📍 Wallet: <code>{FUNDER_ADDRESS[:8]}...{FUNDER_ADDRESS[-6:]}</code>",
        parse_mode=ParseMode.HTML,
    )


# ── /add ────────────────────────────────────────────────────────
@owner_only
async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /add <code>https://polymarket.com/@username</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    raw = " ".join(context.args)
    full_text = update.message.text or ""
    url_match = re.search(r'https?://(?:www\.)?polymarket\.com/[@\w/]+', full_text)
    if url_match:
        raw = url_match.group(0)

    identifier = extract_address_or_username(raw)
    msg = await update.message.reply_text(f"🔍 Resolving <code>{identifier}</code>...", parse_mode=ParseMode.HTML)

    async with aiohttp.ClientSession() as session:
        address = await resolve_username_to_address(session, identifier)
        if not address:
            await msg.edit_text(
                f"❌ Could not resolve <code>{identifier}</code>.\nTry wallet address (0x...).",
                parse_mode=ParseMode.HTML,
            )
            return

        profile = await get_profile(session, address)
        username = identifier
        profile_url = f"https://polymarket.com/@{identifier}"
        if profile:
            username = profile.get("pseudonym") or profile.get("name") or identifier
            if profile.get("pseudonym"):
                profile_url = f"https://polymarket.com/@{profile['pseudonym']}"
            else:
                profile_url = f"https://polymarket.com/profile/{address}"

        added = add_trader(address, username, profile_url)
        if not added:
            update_trader(address, username=username, profile_url=profile_url)
            await msg.edit_text(f"⚠️ <b>{username}</b> already tracked. Updated info.", parse_mode=ParseMode.HTML)
            return

        activities = await get_activity(session, address, limit=100)
        existing = [(a.get("transactionHash", ""), int(a.get("timestamp", 0)))
                     for a in activities if a.get("transactionHash")]
        if existing:
            seed_existing_trades(address, existing)

    await msg.edit_text(
        f"✅ Now tracking <b>{username}</b>\n"
        f"🔗 <a href=\"{profile_url}\">View Profile</a>\n"
        f"<code>{address}</code>\n\n"
        f"📊 {len(existing)} existing trades skipped.\n"
        f"💡 Set a nickname: /nick {username} MyNickname",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


# ── /nick ──────────────────────────────────────────────────────
@owner_only
async def nick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /nick <code>trader_name</code> <code>NewNickname</code>\n"
            "Example: /nick Glass-Typewriter Сашко",
            parse_mode=ParseMode.HTML,
        )
        return

    trader_name = context.args[0]
    nickname = " ".join(context.args[1:])

    trader = find_trader_by_name(trader_name)
    if not trader:
        await update.message.reply_text(f"❌ Trader <b>{trader_name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    set_nickname(trader["address"], nickname)
    old_name = trader.get("username") or trader["address"][:10]
    await update.message.reply_text(
        f"✅ Nickname set!\n<b>{old_name}</b> → <b>{nickname}</b>",
        parse_mode=ParseMode.HTML,
    )


# ── /autocopy ──────────────────────────────────────────────────
@owner_only
async def autocopy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        # Show current autocopy status
        traders = get_all_traders()
        lines = ["<b>🤖 Autocopy Status:</b>\n"]
        for t in traders:
            name = get_display_name(t)
            if t.get("autocopy"):
                import json
                tags = []
                if t.get("autocopy_tags"):
                    try: tags = json.loads(t["autocopy_tags"])
                    except: pass
                tag_str = ", ".join(tags) if tags else "всі"
                lines.append(f"  {name}: ✅ ON ({tag_str})")
            else:
                lines.append(f"  {name}: ❌ OFF")
        lines.append(f"\nUsage: /autocopy <code>name ON/OFF</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /autocopy <code>name ON/OFF</code>\n"
            "Example: /autocopy Glass-Typewriter ON",
            parse_mode=ParseMode.HTML,
        )
        return

    trader_name = context.args[0]
    action = context.args[1].upper()

    trader = find_trader_by_name(trader_name)
    if not trader:
        await update.message.reply_text(f"❌ Trader <b>{trader_name}</b> not found.", parse_mode=ParseMode.HTML)
        return

    if action in ("OFF", "0", "NO", "FALSE"):
        set_autocopy(trader["address"], False)
        name = get_display_name(trader)
        await update.message.reply_text(f"❌ <b>Autocopy OFF</b> for {name}", parse_mode=ParseMode.HTML)
        return

    if action in ("ON", "1", "YES", "TRUE"):
        # Store trader address for tag selection
        context.user_data["autocopy_trader"] = trader["address"]
        name = get_display_name(trader)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🏛 #політика", callback_data="at:#політика"),
                InlineKeyboardButton("₿ #крипто", callback_data="at:#крипто"),
            ],
            [
                InlineKeyboardButton("⚽ #спорт", callback_data="at:#спорт"),
                InlineKeyboardButton("📈 #акції", callback_data="at:#акції"),
            ],
            [
                InlineKeyboardButton("🌡 #погода", callback_data="at:#погода"),
                InlineKeyboardButton("🤖 #ai", callback_data="at:#ai"),
            ],
            [
                InlineKeyboardButton("🌍 #геополітика", callback_data="at:#геополітика"),
                InlineKeyboardButton("🔬 #наука", callback_data="at:#наука"),
            ],
            [
                InlineKeyboardButton("🎬 #культура", callback_data="at:#культура"),
                InlineKeyboardButton("📋 #інше", callback_data="at:#інше"),
            ],
            [
                InlineKeyboardButton("✅ ВСІ НАПРЯМКИ", callback_data="at:ALL"),
            ],
            [
                InlineKeyboardButton("💾 Зберегти вибір", callback_data="at:SAVE"),
            ],
        ])

        context.user_data["autocopy_selected_tags"] = []

        await update.message.reply_text(
            f"🤖 <b>Autocopy для {name}</b>\n\n"
            f"Обери напрямки для копіювання:\n"
            f"(натискай кілька, потім 💾 Зберегти)\n\n"
            f"Обрано: <i>нічого</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text("Use ON or OFF")


# ── /events ─────────────────────────────────────────────────────
@owner_only
async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Interactive event filter management."""
    from database import get_autocopy_event_slugs

    traders = get_all_traders()
    autocopy_traders = [t for t in traders if t.get("autocopy")]

    # If no autocopy traders, try all traders
    if not autocopy_traders:
        autocopy_traders = traders

    if not autocopy_traders:
        await update.message.reply_text("❌ Нема трейдерів. Додай через /add")
        return

    # If only 1 trader — skip selection, go straight to add mode
    if len(autocopy_traders) == 1:
        t = autocopy_traders[0]
        context.user_data["ev_add_trader"] = t["address"]
        name = get_display_name(t)
        slugs = get_autocopy_event_slugs(t["address"])

        lines = [f"📅 <b>Events для {name}:</b>\n"]
        buttons = []

        if slugs:
            for s in slugs:
                lines.append(f"  • <code>{s}</code>")
                addr_short = t["address"][:10]
                buttons.append([
                    InlineKeyboardButton(f"🗑 {s[:40]}", callback_data=f"ev_rm:{addr_short}|{s[:50]}"),
                ])
        else:
            lines.append("  Фільтри не встановлені — копіюємо ВСІ події")

        lines.append("\n👇 <b>Скинь силку на подію з Polymarket щоб додати:</b>")

        addr_short = t["address"][:10]
        if slugs:
            buttons.append([
                InlineKeyboardButton("🗑 Очистити всі фільтри", callback_data=f"ev_clear:{addr_short}"),
            ])

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )
        return

    # Multiple traders — show selection buttons
    lines = ["<b>📅 Event Filters:</b>\n"]
    buttons = []

    for t in autocopy_traders:
        name = get_display_name(t)
        slugs = get_autocopy_event_slugs(t["address"])
        if slugs:
            lines.append(f"<b>{name}:</b>")
            for s in slugs:
                lines.append(f"  • <code>{s}</code>")
        else:
            lines.append(f"<b>{name}:</b> всі події")

        addr_short = t["address"][:10]
        buttons.append([
            InlineKeyboardButton(f"➕ Додати event для {name}", callback_data=f"ev_add:{addr_short}"),
        ])
        if slugs:
            buttons.append([
                InlineKeyboardButton(f"🗑 Очистити фільтри {name}", callback_data=f"ev_clear:{addr_short}"),
            ])

    lines.append("\nНатисни ➕ або скинь силку на подію")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── /snipe90 ────────────────────────────────────────────────────
@owner_only
async def snipe90_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage 90¢ sniper on Elon tweet markets."""
    from sniper90 import (
        fetch_elon_events, get_market_prices, fetch_event_markets,
        add_snipe_event, remove_snipe_event, get_enabled_snipe_events,
        get_sniper90_status,
    )

    if not context.args:
        # Show status + available events
        status = get_sniper90_status()
        events = fetch_elon_events()

        buttons = []
        enabled = get_enabled_snipe_events()

        if events:
            status += "\n\n<b>📋 Доступні events:</b>\n"
            for ev in events[:10]:
                slug = ev.get("slug", "")
                title = ev.get("title", slug)[:50]
                is_on = slug in enabled
                emoji = "✅" if is_on else "⬜"
                status += f"\n{emoji} {title}"
                cb = f"s90_off:{slug[:50]}" if is_on else f"s90_on:{slug[:50]}"
                btn_text = f"{'🔴 OFF' if is_on else '🟢 ON'} {title[:30]}"
                buttons.append([InlineKeyboardButton(btn_text, callback_data=cb)])

        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(status, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    await update.message.reply_text("Використовуй /snipe90 без аргументів — обирай кнопками")


# ── /remove ─────────────────────────────────────────────────────
@owner_only
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /remove <code>username</code>", parse_mode=ParseMode.HTML)
        return

    raw = " ".join(context.args)
    identifier = extract_address_or_username(raw)

    # Try find by nickname/username first
    trader = find_trader_by_name(raw)
    if trader:
        removed = remove_trader(trader["address"])
        identifier = get_display_name(trader)
    elif identifier.startswith("0x"):
        removed = remove_trader(identifier)
    else:
        removed = False

    if removed:
        await update.message.reply_text(f"🗑 Removed <b>{identifier}</b>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"❌ <b>{identifier}</b> not found.", parse_mode=ParseMode.HTML)


# ── /list ───────────────────────────────────────────────────────
@owner_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    traders = get_all_traders()
    if not traders:
        await update.message.reply_text("📭 Watchlist empty. Use /add to start.")
        return

    lines = [f"📋 <b>Watchlist ({len(traders)}):</b>\n"]
    buttons = []

    for i, t in enumerate(traders, 1):
        name = get_display_name(t)
        addr = t["address"]
        purl = t.get("profile_url") or f"https://polymarket.com/profile/{addr}"
        short = f"{addr[:6]}...{addr[-4:]}"
        autocopy = " 🤖" if t.get("autocopy") else ""
        nick_info = f" (aka {t['username']})" if t.get("nickname") and t.get("username") else ""

        lines.append(f"{i}. <b>{name}</b>{nick_info}{autocopy}\n   <a href=\"{purl}\">🔗 Profile</a> · <code>{short}</code>")
        buttons.append([
            InlineKeyboardButton(f"❌ {name}", callback_data=f"rm:{addr[:20]}"),
            InlineKeyboardButton(f"🔍 {name}", callback_data=f"ck:{addr[:20]}"),
        ])

    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


# ── /check ──────────────────────────────────────────────────────
@owner_only
async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    traders = get_all_traders()
    if not context.args:
        if not traders:
            await update.message.reply_text("No traders to check.")
            return
        targets = [(t["address"], get_display_name(t)) for t in traders]
    else:
        raw = " ".join(context.args)
        trader = find_trader_by_name(raw)
        if trader:
            targets = [(trader["address"], get_display_name(trader))]
        else:
            ident = extract_address_or_username(raw)
            targets = [(ident, None)]

    async with aiohttp.ClientSession() as session:
        for addr, uname in targets:
            activities = await get_activity(session, addr, limit=5)
            if not activities:
                await update.message.reply_text(f"No recent activity for {uname or addr[:10]}")
                continue
            for act in activities[:5]:
                side = act.get("side", "")
                act_type = act.get("type", "")
                hashtag = detect_hashtag(act.get("title", ""))
                if act_type == "TRADE" and side == "BUY":
                    text = format_buy_message(act, uname or "?", hashtag=hashtag)
                elif act_type == "TRADE" and side == "SELL":
                    text = format_sell_message(act, uname or "?", hashtag=hashtag)
                else:
                    text = format_other_message(act, uname or "?")
                await update.message.reply_text(
                    text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ── /portfolio ──────────────────────────────────────────────────
@owner_only
async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    copies = get_all_open_copy_trades()
    traders = {t["address"]: get_display_name(t) for t in get_all_traders()}

    if not copies:
        await update.message.reply_text("📭 No open copy-trades.\nUse the Copy Trade button or /autocopy.")
        return

    lines = [f"💼 <b>Your Portfolio ({len(copies)} open):</b>\n"]
    for c in copies:
        tname = traders.get(c["trader_address"], "?")
        source = "🤖" if c.get("source") == "autocopy" else "👆"
        lines.append(
            f"{source} <b>{c.get('title', '?')[:40]}</b>\n"
            f"   {c['outcome']} @ {_price(c['buy_price'])} · "
            f"{_usd(c['usdc_spent'])} · Copying: {tname}"
        )

    balance = get_balance()
    if balance is not None:
        lines.append(f"\n💰 Balance: {_usd(balance)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── /balance ───────────────────────────────────────────────────
@owner_only
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("💰 Рахую...")

    # Cash balance
    cash = get_balance()
    cash_text = _usd(cash) if cash is not None else "❌ не вдалось"

    # Open positions value
    copies = get_all_open_copy_trades()
    total_invested = 0.0
    total_current = 0.0
    total_unrealized = 0.0
    position_lines = []

    if copies:
        async with aiohttp.ClientSession() as session:
            for c in copies:
                invested = float(c.get("usdc_spent", 0))
                total_invested += invested

                # Get current price
                token_id = c.get("token_id", "")
                cur_price = None
                if token_id:
                    try:
                        url = f"https://clob.polymarket.com/midpoint?token_id={token_id}"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                mid = data.get("mid")
                                if mid:
                                    cur_price = float(mid)
                    except Exception:
                        pass

                shares = float(c.get("shares", 0))
                if cur_price:
                    cur_val = shares * cur_price
                    unrealized = cur_val - invested
                    total_current += cur_val
                    total_unrealized += unrealized
                    sign = "+" if unrealized >= 0 else ""
                    emoji = "🟩" if unrealized >= 0 else "🟥"
                    position_lines.append(
                        f"  {emoji} {c.get('title', '?')[:35]}\n"
                        f"     {_usd(invested)} → {_usd(cur_val)} ({sign}{_usd(unrealized)})"
                    )
                else:
                    total_current += invested  # fallback
                    position_lines.append(
                        f"  ❓ {c.get('title', '?')[:35]}\n"
                        f"     {_usd(invested)} (ціна невідома)"
                    )

    # Closed P&L
    from database import get_closed_copy_trades
    closed = get_closed_copy_trades(limit=999)
    total_realized = sum(float(c.get("pnl_usdc", 0)) for c in closed)
    total_closed_count = len(closed)
    wins = sum(1 for c in closed if float(c.get("pnl_usdc", 0)) > 0)
    winrate = (wins / total_closed_count * 100) if total_closed_count > 0 else 0

    # Build message
    total_value = (cash or 0) + total_current
    lines = [
        f"💰 <b>Баланс</b>\n",
        f"💵 Кеш: <b>{cash_text}</b>",
        f"📊 В угодах: <b>{_usd(total_current)}</b> ({len(copies)} позицій)",
        f"💎 Всього: <b>{_usd(total_value)}</b>",
    ]

    if total_unrealized != 0:
        sign = "+" if total_unrealized >= 0 else ""
        emoji = "🟩" if total_unrealized >= 0 else "🟥"
        lines.append(f"\n{emoji} Нереалізований P&L: <b>{sign}{_usd(total_unrealized)}</b>")

    if total_closed_count > 0:
        sign = "+" if total_realized >= 0 else ""
        emoji = "🟩" if total_realized >= 0 else "🟥"
        lines.append(
            f"{emoji} Реалізований P&L: <b>{sign}{_usd(total_realized)}</b>"
            f" ({total_closed_count} угод, {winrate:.0f}% win)"
        )

    if position_lines:
        lines.append(f"\n<b>Відкриті позиції ({len(position_lines)}):</b>")
        # Show max 10 positions to avoid Message_too_long
        for pl in position_lines[:10]:
            lines.append(pl)
        if len(position_lines) > 10:
            lines.append(f"  ... і ще {len(position_lines) - 10}")

    text = "\n".join(lines)
    # Telegram max is 4096 chars
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (обрізано)"

    await msg.edit_text(text, parse_mode=ParseMode.HTML)


# ── /cleanup ───────────────────────────────────────────────────
@owner_only
async def cleanup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel live PENDING orders, verify OPEN positions. Never deletes OPEN trades."""
    from trading import check_order_status, cancel_order
    from database import get_all_pending_copy_trades, update_copy_trade_status

    # Only clean PENDING trades (not yet filled)
    pending = get_all_pending_copy_trades()
    copies = get_all_open_copy_trades()

    if not pending and not copies:
        await update.message.reply_text("✅ Нема відкритих/pending копі-трейдів.")
        return

    msg = await update.message.reply_text(
        f"🧹 Перевіряю {len(pending)} pending + {len(copies)} open...")

    cleaned = 0
    cancelled = 0
    confirmed = 0

    # Process PENDING trades — these can be cleaned
    for c in pending:
        order_id = c.get("order_id", "")
        if order_id:
            status = check_order_status(order_id)
            status_lower = status.lower() if status else ""

            if status_lower == "matched":
                update_copy_trade_status(c["id"], "OPEN")
                confirmed += 1
            elif status_lower == "live":
                cancel_order(order_id)
                update_copy_trade_status(c["id"], "CANCELLED")
                cancelled += 1
            else:
                # Not matched, not live → ghost pending
                update_copy_trade_status(c["id"], "CANCELLED")
                cleaned += 1
        else:
            # No order_id → ghost
            update_copy_trade_status(c["id"], "CANCELLED")
            cleaned += 1

    # OPEN trades — NEVER delete, just report
    open_count = len(copies)

    await msg.edit_text(
        f"🧹 <b>Cleanup done!</b>\n\n"
        f"✅ Open позиції (не чіпав): {open_count}\n"
        f"✅ Pending → підтверджено: {confirmed}\n"
        f"❌ Pending → скасовано: {cancelled}\n"
        f"🗑 Pending → привиди: {cleaned}",
        parse_mode=ParseMode.HTML,
    )


# ── /reset_pnl ─────────────────────────────────────────────────
@owner_only
async def reset_pnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all fake P&L data from ghost trades."""
    from database import get_db
    conn = get_db()
    # Delete all closed copy trades (they're mostly ghosts from cleanup)
    cursor = conn.execute("DELETE FROM copy_trades WHERE status = 'CLOSED'")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🧹 <b>P&L Reset!</b>\n\n"
        f"Видалено {deleted} закритих записів.\n"
        f"Тепер /balance покаже чисту статистику.",
        parse_mode=ParseMode.HTML,
    )


# ── Callback handler ────────────────────────────────────────────
@owner_only
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    # ── Event filter: add event ──
    if data.startswith("ev_add:"):
        addr_prefix = data[7:]
        traders = get_all_traders()
        found = next((t for t in traders if t["address"].startswith(addr_prefix)), None)
        if found:
            context.user_data["ev_add_trader"] = found["address"]
            name = get_display_name(found)
            await query.edit_message_text(
                f"📅 <b>Додати event для {name}</b>\n\n"
                f"Скинь силку на подію з Polymarket:\n"
                f"<code>https://polymarket.com/event/...</code>",
                parse_mode=ParseMode.HTML,
            )
        return

    # ── Event filter: clear all ──
    if data.startswith("ev_clear:"):
        addr_prefix = data[9:]
        traders = get_all_traders()
        found = next((t for t in traders if t["address"].startswith(addr_prefix)), None)
        if found:
            from database import set_autocopy_event_slugs
            set_autocopy_event_slugs(found["address"], "")
            name = get_display_name(found)
            await query.edit_message_text(
                f"✅ <b>{name}</b> — фільтри очищені\nКопіюємо ВСІ події",
                parse_mode=ParseMode.HTML,
            )
        return

    # ── Event filter: remove specific slug ──
    if data.startswith("ev_rm:"):
        parts = data[6:].split("|", 1)
        if len(parts) == 2:
            addr_prefix, slug = parts
            traders = get_all_traders()
            found = next((t for t in traders if t["address"].startswith(addr_prefix)), None)
            if found:
                from database import get_autocopy_event_slugs, set_autocopy_event_slugs
                slugs = get_autocopy_event_slugs(found["address"])
                slugs = [s for s in slugs if s != slug]
                set_autocopy_event_slugs(found["address"], ",".join(slugs))
                name = get_display_name(found)
                if slugs:
                    slug_list = "\n".join(f"  • <code>{s}</code>" for s in slugs)
                    await query.edit_message_text(
                        f"🗑 Видалено: <code>{slug}</code>\n\n"
                        f"<b>{name}</b> залишились:\n{slug_list}",
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await query.edit_message_text(
                        f"🗑 Видалено останній фільтр\n<b>{name}</b> — копіюємо ВСІ події",
                        parse_mode=ParseMode.HTML,
                    )
        return

    # ── Snipe90 toggle ──
    if data.startswith("s90_on:"):
        slug = data[7:]
        from sniper90 import add_snipe_event
        add_snipe_event(slug)
        await query.edit_message_text(
            f"✅ <b>Sniper 90¢ ON</b>\n<code>{slug}</code>\n\n"
            f"Лімітки будуть поставлені за 48h до кінця",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("s90_off:"):
        slug = data[8:]
        from sniper90 import remove_snipe_event
        remove_snipe_event(slug)
        await query.edit_message_text(
            f"🔴 <b>Sniper 90¢ OFF</b>\n<code>{slug}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Remove via button ──
    if data.startswith("rm:"):
        addr_prefix = data[3:]
        traders = get_all_traders()
        found = next((t for t in traders if t["address"].startswith(addr_prefix)), None)
        if found:
            remove_trader(found["address"])
            name = get_display_name(found)
            await query.edit_message_text(f"🗑 Removed <b>{name}</b>", parse_mode=ParseMode.HTML)
        else:
            await query.edit_message_text("❌ Not found.")

    # ── Check via button ──
    elif data.startswith("ck:"):
        addr_prefix = data[3:]
        traders = get_all_traders()
        found = next((t for t in traders if t["address"].startswith(addr_prefix)), None)
        if found:
            await query.edit_message_text(f"🔍 Checking {get_display_name(found)}...")
            async with aiohttp.ClientSession() as session:
                activities = await get_activity(session, found["address"], limit=3)
                name = get_display_name(found)
                for act in activities[:3]:
                    side = act.get("side", "")
                    act_type = act.get("type", "")
                    hashtag = detect_hashtag(act.get("title", ""))
                    if act_type == "TRADE" and side == "BUY":
                        text = format_buy_message(act, name, hashtag=hashtag)
                    elif act_type == "TRADE" and side == "SELL":
                        text = format_sell_message(act, name, hashtag=hashtag)
                    else:
                        text = format_other_message(act, name)
                    await query.message.reply_text(
                        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    # ── Autocopy tag selection ──
    elif data.startswith("at:"):
        val = data[3:]
        trader_addr = context.user_data.get("autocopy_trader")
        if not trader_addr:
            await query.edit_message_text("⏰ Expired. Run /autocopy again.")
            return

        selected = context.user_data.get("autocopy_selected_tags", [])

        if val == "ALL":
            selected = []  # empty = all
            context.user_data["autocopy_selected_tags"] = selected
            # Save immediately
            from database import set_autocopy_tags
            set_autocopy(trader_addr, True)
            set_autocopy_tags(trader_addr, [])
            trader = find_trader_by_name(trader_addr) or {}
            name = get_display_name(trader) if trader else trader_addr[:10]
            await query.edit_message_text(
                f"✅ <b>Autocopy ON</b> for {name}\n"
                f"📋 Напрямки: <b>всі</b>\n\n"
                f"💰 Rules: &lt;$1 exact, $2-10→$1, $10-20→$2, $20-50→$3, $50+→$5 (1x/day)\n"
                f"🤖 Auto-sell when trader exits.",
                parse_mode=ParseMode.HTML,
            )
            return

        if val == "SAVE":
            if not selected:
                await query.answer("Обери хоча б один напрямок або 'ВСІ'", show_alert=True)
                return
            from database import set_autocopy_tags
            set_autocopy(trader_addr, True)
            set_autocopy_tags(trader_addr, selected)
            trader = find_trader_by_name(trader_addr) or {}
            name = get_display_name(trader) if trader else trader_addr[:10]
            await query.edit_message_text(
                f"✅ <b>Autocopy ON</b> for {name}\n"
                f"📋 Напрямки: <b>{', '.join(selected)}</b>\n\n"
                f"💰 Rules: &lt;$1 exact, $2-10→$1, $10-20→$2, $20-50→$3, $50+→$5 (1x/day)\n"
                f"🤖 Auto-sell when trader exits.",
                parse_mode=ParseMode.HTML,
            )
            return

        # Toggle tag
        tag = val
        if tag in selected:
            selected.remove(tag)
        else:
            selected.append(tag)
        context.user_data["autocopy_selected_tags"] = selected

        selected_text = ", ".join(selected) if selected else "<i>нічого</i>"
        await query.answer(f"{'✅' if tag in selected else '❌'} {tag}")

        # Rebuild keyboard with checkmarks
        all_tags = [
            ("#політика", "🏛"), ("#крипто", "₿"), ("#спорт", "⚽"), ("#акції", "📈"),
            ("#погода", "🌡"), ("#ai", "🤖"), ("#геополітика", "🌍"), ("#наука", "🔬"),
            ("#культура", "🎬"), ("#інше", "📋"),
        ]
        rows = []
        for i in range(0, len(all_tags), 2):
            row = []
            for tag_name, emoji in all_tags[i:i+2]:
                check = "✅ " if tag_name in selected else ""
                row.append(InlineKeyboardButton(f"{check}{emoji} {tag_name}", callback_data=f"at:{tag_name}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("✅ ВСІ НАПРЯМКИ", callback_data="at:ALL")])
        rows.append([InlineKeyboardButton("💾 Зберегти вибір", callback_data="at:SAVE")])

        trader = find_trader_by_name(trader_addr) or {}
        name = get_display_name(trader) if trader else trader_addr[:10]
        await query.edit_message_text(
            f"🤖 <b>Autocopy для {name}</b>\n\n"
            f"Обери напрямки для копіювання:\n"
            f"(натискай кілька, потім 💾 Зберегти)\n\n"
            f"Обрано: {selected_text}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    # ── Copy Trade — step 1: show amount picker ──
    elif data.startswith("ct:"):
        trade_hash = data[3:]
        trade_info = pending_copy_data.get(trade_hash)
        if not trade_info:
            await query.edit_message_text("⏰ Trade data expired. Can't copy this one.")
            return

        context.user_data["pending_copy"] = trade_info
        context.user_data["pending_hash"] = trade_hash

        balance = get_balance()
        bal_text = _usd(balance) if balance is not None else "unknown"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("$1", callback_data="ca:1"),
                InlineKeyboardButton("$5", callback_data="ca:5"),
                InlineKeyboardButton("$25", callback_data="ca:25"),
                InlineKeyboardButton("$100", callback_data="ca:100"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="ca:cancel")],
        ])

        price = trade_info["price"]
        await query.message.reply_text(
            f"💰 <b>Copy Trade</b>\n\n"
            f"📌 <b>{trade_info['title']}</b>\n"
            f"🎯 {trade_info['outcome']} @ {_price(price)}\n"
            f"👤 Copying: {trade_info['trader_name']}\n\n"
            f"💼 Your balance: ~{bal_text}\n\n"
            f"How much USDC to spend?\n"
            f"(Or type a custom amount)",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    # ── Copy Trade — step 2: amount selected ──
    elif data.startswith("ca:"):
        val = data[3:]
        if val == "cancel":
            context.user_data.pop("pending_copy", None)
            await query.edit_message_text("❌ Cancelled.")
            return

        amount = float(val)
        trade_info = context.user_data.get("pending_copy")
        if not trade_info:
            await query.edit_message_text("⏰ Expired. Try again.")
            return

        context.user_data["copy_amount"] = amount
        price = trade_info["price"]
        est_shares = amount / price if price > 0 else 0

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm", callback_data="cx:yes"),
                InlineKeyboardButton("❌ Cancel", callback_data="cx:no"),
            ]
        ])

        await query.edit_message_text(
            f"⚠️ <b>Confirm Order:</b>\n\n"
            f"📌 <b>{trade_info['title']}</b>\n"
            f"🎯 BUY {trade_info['outcome']} @ {_price(price)} (limit)\n"
            f"💵 Spend: {_usd(amount)}\n"
            f"📊 Est. shares: ~{_shares(est_shares)}\n\n"
            f"Press Confirm to place the order.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    # ── Copy Trade — step 3: confirm ──
    elif data.startswith("cx:"):
        if data == "cx:no":
            context.user_data.pop("pending_copy", None)
            context.user_data.pop("copy_amount", None)
            await query.edit_message_text("❌ Cancelled.")
            return

        trade_info = context.user_data.pop("pending_copy", None)
        amount = context.user_data.pop("copy_amount", None)
        if not trade_info or not amount:
            await query.edit_message_text("⏰ Expired.")
            return

        await query.edit_message_text("⏳ Placing order...")

        condition_id = trade_info["condition_id"]
        outcome = trade_info["outcome"]
        price = trade_info["price"]
        token_id = trade_info.get("token_id", "")
        hashtag = trade_info.get("hashtag", "")

        if not token_id:
            token_id = get_token_id_for_market(condition_id, outcome) or ""

        if not token_id:
            await query.edit_message_text("❌ Could not find token ID for this market.")
            return

        result = place_fok_buy(token_id, price, amount, condition_id)

        if result:
            shares = result["size"]
            order_id = result.get("order_id", "")

            save_copy_trade(
                trader_address=trade_info["trader_address"],
                condition_id=condition_id,
                token_id=token_id,
                outcome=outcome,
                buy_price=price,
                usdc_spent=amount,
                shares=shares,
                order_id=order_id,
                timestamp=int(time.time()),
                title=trade_info.get("title", ""),
                hashtag=hashtag,
                source="manual",
            )

            await query.edit_message_text(
                f"✅ <b>Order Placed!</b>\n\n"
                f"📌 <b>{trade_info['title']}</b>\n"
                f"🎯 BUY {outcome} @ {_price(price)}\n"
                f"💵 {_usd(amount)} ({_shares(shares)} shares)\n\n"
                f"🤖 Will auto-sell when {trade_info['trader_name']} exits.",
                parse_mode=ParseMode.HTML,
            )
            # Forward to channel
            try:
                from config import CHANNEL_ID
                if CHANNEL_ID:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=(
                            f"🟢 <b>MANUAL COPY BUY</b>\n\n"
                            f"📌 <b>{trade_info['title']}</b>\n"
                            f"🎯 {outcome} @ {_price(price)}\n"
                            f"💵 {_usd(amount)} ({_shares(shares)} shares)\n"
                            f"👤 Copying: {trade_info['trader_name']}"
                        ),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                    )
            except Exception:
                pass
        else:
            await query.edit_message_text(
                f"❌ <b>Order Failed</b>\n\n"
                f"Check logs for details. Make sure:\n"
                f"• PRIVATE_KEY is correct\n"
                f"• You have enough USDC\n"
                f"• Token allowances are set",
                parse_mode=ParseMode.HTML,
            )


# ── Handle custom amount typed by user ──────────────────────────
@owner_only
async def custom_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ── Handle Polymarket URL for event filter ──
    ev_trader = context.user_data.get("ev_add_trader")
    if ev_trader and update.message and update.message.text:
        import re
        text = update.message.text.strip()
        match = re.search(r'polymarket\.com/event/([^/?#\s]+)', text)
        if match:
            slug = match.group(1)
            from database import get_autocopy_event_slugs, set_autocopy_event_slugs
            existing = get_autocopy_event_slugs(ev_trader)
            if slug not in existing:
                existing.append(slug)
            set_autocopy_event_slugs(ev_trader, ",".join(existing))

            traders = get_all_traders()
            found = next((t for t in traders if t["address"] == ev_trader), None)
            name = get_display_name(found) if found else ev_trader[:10]

            # Build buttons for each slug (to remove individually)
            buttons = []
            for s in existing:
                addr_short = ev_trader[:10]
                buttons.append([
                    InlineKeyboardButton(f"🗑 {s[:40]}", callback_data=f"ev_rm:{addr_short}|{s[:50]}"),
                ])
            buttons.append([
                InlineKeyboardButton(f"➕ Додати ще event", callback_data=f"ev_add:{ev_trader[:10]}"),
            ])

            await update.message.reply_text(
                f"✅ <b>Event додано для {name}!</b>\n\n"
                f"<code>{slug}</code>\n\n"
                f"Активні фільтри ({len(existing)}):\n" +
                "\n".join(f"  • <code>{s}</code>" for s in existing),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            context.user_data.pop("ev_add_trader", None)
            return
        elif "polymarket" in text.lower():
            await update.message.reply_text(
                "❌ Не знайшов event slug в URL.\n\n"
                "Потрібна силка виду:\n"
                "<code>https://polymarket.com/event/elon-musk-tweets-...</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    trade_info = context.user_data.get("pending_copy")
    if not trade_info:
        return

    text = update.message.text.strip().replace("$", "").replace(",", "")
    try:
        amount = float(text)
    except ValueError:
        return

    if amount <= 0:
        await update.message.reply_text("Amount must be positive.")
        return

    context.user_data["copy_amount"] = amount
    price = trade_info["price"]
    est_shares = amount / price if price > 0 else 0

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="cx:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="cx:no"),
        ]
    ])

    await update.message.reply_text(
        f"⚠️ <b>Confirm Order:</b>\n\n"
        f"📌 <b>{trade_info['title']}</b>\n"
        f"🎯 BUY {trade_info['outcome']} @ {_price(price)} (limit)\n"
        f"💵 Spend: {_usd(amount)}\n"
        f"📊 Est. shares: ~{_shares(est_shares)}\n\n"
        f"Press Confirm to place the order.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# ── Main ────────────────────────────────────────────────────────
async def post_init(app: Application):
    # Set bot commands menu
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start", "📋 Головне меню"),
        BotCommand("add", "➕ Додати трейдера"),
        BotCommand("remove", "🗑 Видалити трейдера"),
        BotCommand("list", "📋 Список трейдерів"),
        BotCommand("check", "🔍 Останні угоди"),
        BotCommand("balance", "💰 Баланс і P&L"),
        BotCommand("portfolio", "💼 Мої копі-трейди"),
        BotCommand("cleanup", "🧹 Видалити привидні трейди"),
        BotCommand("autocopy", "🤖 Автокопітрейдинг"),
        BotCommand("events", "📅 Фільтр подій для копі"),
        BotCommand("snipe90", "🎯 Sniper 90¢ Маск"),
        BotCommand("status", "📊 Статус бота"),
    ])

    # Start poller
    asyncio.create_task(poll_traders(app.bot))
    logger.info("Poller task created")

    # Start sniper 90¢
    from sniper90 import sniper90_loop
    asyncio.create_task(sniper90_loop(app.bot))
    logger.info("Sniper 90¢ task created")

    # Start order checker (PENDING → OPEN/CANCELLED)
    from poller import check_pending_orders
    asyncio.create_task(check_pending_orders(app.bot))
    logger.info("Order checker task created")

    # Sheets updater — removed in v2

    # Start health monitor
    asyncio.create_task(health_monitor(app.bot))
    logger.info("Health monitor started")

    # Sniper — removed in v2

    # Weather sniper — removed in v2

    # Adaptive BTC — removed in v2

    # MM bot — removed in v2

    # Liquidity scalper — removed in v2

    # Weather trader — removed in v2

    trading = "✅" if is_trading_enabled() else "❌ (no key)"
    try:
        await app.bot.send_message(
            chat_id=OWNER_ID,
            text=f"🤖 <b>Bot v2 started!</b>\n⏱ Polling: 3s\n🔄 Order check: 30s\n🏥 Health: 5min\n💰 Trading: {trading}\n📊 FOK buy + Smart sell",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ── Health monitor ──────────────────────────────────────────────

async def health_monitor(bot):
    """Background task — checks bot health every 5 min."""
    from health import last_poll_time, consecutive_errors

    await asyncio.sleep(120)  # Wait 2 min before first check

    while True:
        try:
            import health
            issues = []

            # Check 1: Poller alive? (should poll every 15s, alert if >120s)
            since_last_poll = time.time() - health.last_poll_time
            if since_last_poll > 120:
                issues.append(f"⚠️ Poller не працює вже {int(since_last_poll)}с")

            # Check 2: Too many consecutive errors?
            if health.consecutive_errors >= 5:
                issues.append(f"⚠️ {health.consecutive_errors} помилок підряд")

            # Check 3: Balance check
            balance = get_balance()
            if balance is not None and balance < 1.0:
                issues.append(f"⚠️ Низький баланс: ${balance:.2f}")

            # Check 4: Trading still enabled?
            if not is_trading_enabled():
                issues.append("⚠️ Трейдинг вимкнений (PRIVATE_KEY)")

            if issues:
                text = "🏥 <b>Health Alert!</b>\n\n" + "\n".join(issues)
                await bot.send_message(
                    chat_id=OWNER_ID, text=text,
                    parse_mode=ParseMode.HTML,
                )

        except Exception as e:
            logger.error(f"Health monitor error: {e}")

        await asyncio.sleep(300)  # Check every 5 min


# ── /snipe — Directional sniper ───────────────────────────────

_snipe_setup: dict = {}  # user_id -> setup state

@owner_only
async def snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start sniper: /snipe <event_url>"""
    if not context.args:
        await update.message.reply_text(
            "🎯 <b>Sniper</b>\n\n"
            "Usage: /snipe <code>polymarket_event_url</code>\n"
            "Example: /snipe https://polymarket.com/event/btc-updown-15m-...\n\n"
            "Ставить лімітку на YES або NO по твоїй ціні.\n"
            "Коли ринок закривається — YES=$1 або $0.",
            parse_mode=ParseMode.HTML,
        )
        return

    url = context.args[0]
    match = re.search(r'polymarket\.com/event/([^\s/?#]+)', url)
    if not match:
        await update.message.reply_text("❌ Невірна силка. Потрібен формат: https://polymarket.com/event/...")
        return

    slug = match.group(1)
    msg = await update.message.reply_text("⏳ Завантажую...")

    try:
        try:
            from sniper import fetch_event_by_slug, fetch_orderbook, get_token_id
        except (ImportError, ModuleNotFoundError):
            pass  # module removed in v2
        import requests

        event = fetch_event_by_slug(slug)
        if not event:
            await msg.edit_text("❌ Не знайшов івент.")
            return

        markets = event.get("markets", [])
        if not markets:
            await msg.edit_text("❌ Івент не має ринків.")
            return

        market = markets[0]
        cid = market.get("conditionId", "")
        title = market.get("question", event.get("title", "?"))

        # Get orderbook for YES
        token_yes = get_token_id(cid, "yes")
        book = fetch_orderbook(token_yes) if token_yes else None

        book_text = ""
        if book:
            book_text = (
                f"\n📖 <b>Orderbook (YES):</b>\n"
                f"   Best Bid: {book['best_bid']*100:.0f}¢ | Best Ask: {book['best_ask']*100:.0f}¢\n"
                f"   Mid: {book['mid']*100:.0f}¢ | Spread: {book['spread']*100:.0f}¢"
            )

        uid = update.effective_user.id
        _snipe_setup[uid] = {
            "event": event,
            "market": market,
            "slug": slug,
            "cid": cid,
            "token_yes": token_yes,
            "book": book,
            "step": "pick_side",
        }

        # Detect market type from slug
        mtype = "15m"
        if "-5m-" in slug: mtype = "5m"
        elif "-1h-" in slug or "1-hour" in slug: mtype = "1h"
        elif "-4h-" in slug: mtype = "4h"
        elif "-daily-" in slug or "on-february" in slug: mtype = "daily"
        _snipe_setup[uid]["market_type"] = mtype

        buttons = [
            [InlineKeyboardButton("🟢 YES / UP", callback_data="snipe_side:YES"),
             InlineKeyboardButton("🔴 NO / DOWN", callback_data="snipe_side:NO")],
        ]
        await msg.edit_text(
            f"🎯 <b>{title[:80]}</b>\n"
            f"⏱ Type: {mtype}"
            f"{book_text}\n\n"
            f"Що купляємо?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except Exception as e:
        await msg.edit_text(f"❌ Помилка: {e}")


@owner_only
async def snipe_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all active snipers + auto-sniper."""
    try:
        from sniper import get_all_sessions, format_session_status, format_auto_status, get_all_auto_snipers
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    snipers = get_all_auto_snipers()
    if snipers:
        await update.message.reply_text(format_auto_status(), parse_mode=ParseMode.HTML)

    sessions = get_all_sessions()
    if not sessions and not snipers:
        await update.message.reply_text("🎯 Немає активних снайперів.")


@owner_only
async def snipe_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop all snipers."""
    try:
        from sniper import stop_all
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    stopped_sessions, stopped_snipers = stop_all()

    if not stopped_sessions and not stopped_snipers:
        await update.message.reply_text("🎯 Немає активних снайперів.")
        return

    text = "🛑 <b>All snipers stopped</b>\n\n"
    total_wins = sum(s.wins for s in stopped_snipers)
    total_losses = sum(s.losses for s in stopped_snipers)
    total_pnl = sum(s.total_pnl for s in stopped_snipers)
    total_trades = sum(s.total_trades for s in stopped_snipers)
    total = total_wins + total_losses
    wr = (total_wins / total * 100) if total > 0 else 0
    sign = "+" if total_pnl >= 0 else ""

    for s in stopped_snipers:
        sw = "+" if s.total_pnl >= 0 else ""
        text += f"• {s.market_type}: {s.wins}W/{s.losses}L | {sw}${s.total_pnl:.2f}\n"

    text += f"\n📈 Total: {total_trades} trades\n🏆 {total_wins}W / {total_losses}L ({wr:.0f}%)\n💰 P&L: {sign}${total_pnl:.2f}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@owner_only
async def snipe_auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start auto-sniper: /snipe_auto"""
    try:
        from sniper import get_all_auto_snipers
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    existing = get_all_auto_snipers()
    types = [s.market_type for s in existing]

    uid = update.effective_user.id
    _snipe_setup[uid] = {"mode": "auto", "step": "pick_type", "existing_types": types}

    buttons = [
        [InlineKeyboardButton("⚡ 15 min", callback_data="snipe_type:15m"),
         InlineKeyboardButton("⏱ 1 hour", callback_data="snipe_type:1h")],
    ]
    await update.message.reply_text(
        "🤖 <b>Auto-Sniper Setup</b>\n\n"
        "Бот автоматично:\n"
        "1. Чекає до останніх хвилин ринку\n"
        "2. Дивиться BTC на Binance\n"
        "3. Якщо BTC чітко йде вгору/вниз → ставить лімітку\n"
        "4. Стоп-лос якщо ціна розвернулась\n"
        "5. Переходить на наступний ринок\n\n"
        "Вибери тип ринку:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def snipe_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle sniper setup inline buttons."""
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if uid != OWNER_ID:
        return

    data = query.data
    setup = _snipe_setup.get(uid)
    if not setup:
        await query.edit_message_text("❌ Сесія закінчилась. Почни знову: /snipe")
        return

    # ── Auto: pick market type ─────────────────────────────
    if data.startswith("snipe_type:"):
        mtype = data.split(":")[1]
        existing = setup.get("existing_types", [])
        if mtype in existing:
            # Replace existing sniper of same type
            try:
                from sniper import stop_auto_sniper
            except (ImportError, ModuleNotFoundError):
                pass  # module removed in v2
            stop_auto_sniper(mtype)

        setup["market_type"] = mtype
        setup["step"] = "auto_price"

        buttons = [
            [InlineKeyboardButton("65¢", callback_data="snipe_aprice:65"),
             InlineKeyboardButton("70¢", callback_data="snipe_aprice:70")],
            [InlineKeyboardButton("75¢", callback_data="snipe_aprice:75"),
             InlineKeyboardButton("80¢", callback_data="snipe_aprice:80")],
            [InlineKeyboardButton("85¢", callback_data="snipe_aprice:85"),
             InlineKeyboardButton("88¢", callback_data="snipe_aprice:88")],
        ]
        enter_sec = 180 if mtype == "15m" else 300
        await query.edit_message_text(
            f"⏱ {mtype} | Входимо за {enter_sec}с до кінця\n\n"
            f"Ціна входу (лімітка):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("snipe_aprice:"):
        price = int(data.split(":")[1])
        setup["price"] = price / 100
        setup["step"] = "auto_size"

        buttons = [
            [InlineKeyboardButton("$1", callback_data="snipe_asize:1"),
             InlineKeyboardButton("$2", callback_data="snipe_asize:2")],
            [InlineKeyboardButton("$3", callback_data="snipe_asize:3"),
             InlineKeyboardButton("$5", callback_data="snipe_asize:5")],
        ]
        await query.edit_message_text(
            f"⏱ {setup['market_type']} | Entry: {price}¢\n\nРозмір:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("snipe_asize:"):
        size = float(data.split(":")[1])
        setup["size"] = size
        setup["step"] = "auto_stoploss"

        buttons = [
            [InlineKeyboardButton("5¢", callback_data="snipe_asl:5"),
             InlineKeyboardButton("10¢", callback_data="snipe_asl:10")],
            [InlineKeyboardButton("15¢", callback_data="snipe_asl:15"),
             InlineKeyboardButton("❌ Без SL", callback_data="snipe_asl:0")],
        ]
        await query.edit_message_text(
            f"⏱ {setup['market_type']} | {int(setup['price']*100)}¢ | ${size:.2f}\n\nСтоп-лос:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("snipe_asl:"):
        sl = int(data.split(":")[1])
        setup["stop_loss"] = sl
        setup["step"] = "auto_timing"

        mtype = setup["market_type"]
        buttons = [
            [InlineKeyboardButton("30с", callback_data="snipe_atime:30"),
             InlineKeyboardButton("60с", callback_data="snipe_atime:60")],
            [InlineKeyboardButton("120с", callback_data="snipe_atime:120"),
             InlineKeyboardButton("180с", callback_data="snipe_atime:180")],
        ]
        if mtype == "1h":
            buttons = [
                [InlineKeyboardButton("60с", callback_data="snipe_atime:60"),
                 InlineKeyboardButton("120с", callback_data="snipe_atime:120")],
                [InlineKeyboardButton("180с", callback_data="snipe_atime:180"),
                 InlineKeyboardButton("300с", callback_data="snipe_atime:300")],
            ]

        await query.edit_message_text(
            f"⏱ {mtype} | {int(setup['price']*100)}¢ | SL: {sl}¢\n\n"
            f"За скільки до кінця входити?\n\n"
            f"30с = рідко fill, але точний\n"
            f"60с = баланс точності і fill\n"
            f"120с = частіше fill\n"
            f"180с = найчастіше fill, але ризик розвороту",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("snipe_atime:"):
        enter_sec = int(data.split(":")[1])
        setup["enter_sec"] = enter_sec
        setup["step"] = "auto_btc_trigger"

        buttons = [
            [InlineKeyboardButton("0.01%", callback_data="snipe_abtc:0.01"),
             InlineKeyboardButton("0.03%", callback_data="snipe_abtc:0.03")],
            [InlineKeyboardButton("0.05%", callback_data="snipe_abtc:0.05"),
             InlineKeyboardButton("0.10%", callback_data="snipe_abtc:0.10")],
        ]
        await query.edit_message_text(
            f"⏱ {setup['market_type']} | {int(setup['price']*100)}¢ | SL: {setup['stop_loss']}¢ | {enter_sec}с\n\n"
            f"Мін. рух BTC щоб увійти:\n\n"
            f"0.01% = входить майже завжди (~$10 рух)\n"
            f"0.03% = помірний фільтр (~$30 рух)\n"
            f"0.05% = строгий (~$50 рух)\n"
            f"0.10% = тільки сильний рух (~$100)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("snipe_abtc:"):
        btc_trigger = float(data.split(":")[1])
        setup["btc_trigger"] = btc_trigger

        # ── CONFIRM ───────────────────────────────────────
        mtype = setup["market_type"]
        price = setup["price"]
        size = setup["size"]
        sl = setup["stop_loss"]
        enter_sec = setup["enter_sec"]
        shares = round(size / price, 2)
        profit = round(shares * (1 - price), 2)
        loss_with_sl = round(shares * (sl / 100), 2) if sl > 0 else round(size, 2)
        loss_label = f"-${loss_with_sl:.2f} (SL {sl}¢)" if sl > 0 else f"-${size:.2f} (без SL)"

        buttons = [
            [InlineKeyboardButton("🤖 ЗАПУСТИТИ", callback_data="snipe_ago:yes"),
             InlineKeyboardButton("❌ Скасувати", callback_data="snipe_ago:no")],
        ]
        await query.edit_message_text(
            f"🤖 <b>Auto-Sniper — Confirm</b>\n\n"
            f"⏱ Ринок: BTC Up/Down {mtype}\n"
            f"🎯 Entry: {int(price*100)}¢ | ${size:.2f} = {shares:.1f} shares\n"
            f"⏰ Входити за {enter_sec}с до кінця\n"
            f"📊 Тригер: BTC рух ≥{btc_trigger:.2f}% на Binance\n"
            f"🛡 Stop-loss: {sl}¢{' (вимкнено)' if sl == 0 else ''}\n"
            f"✅ Win: +${profit:.2f} | ❌ Loss: {loss_label}\n"
            f"🔒 Momentum: входить тільки коли ціна росте\n\n"
            f"Автоматично входить в кожний ринок 24/7.\n"
            f"Запускаємо?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("snipe_ago:"):
        choice = data.split(":")[1]
        if choice == "no":
            _snipe_setup.pop(uid, None)
            await query.edit_message_text("❌ Скасовано.")
            return

        try:
            from sniper import start_auto_sniper, format_auto_status
        except (ImportError, ModuleNotFoundError):
            pass  # module removed in v2

        mtype = setup["market_type"]
        price = setup["price"]
        size = setup["size"]
        sl = setup["stop_loss"]
        enter_sec = setup.get("enter_sec", 180)
        btc_trigger = setup.get("btc_trigger", 0.03)

        auto = start_auto_sniper(
            market_type=mtype,
            entry_price=price,
            size_usdc=size,
            stop_loss_cents=sl,
            enter_before_sec=enter_sec,
            min_btc_move_pct=btc_trigger,
        )

        _snipe_setup.pop(uid, None)

        await query.edit_message_text(
            f"🤖 <b>Auto-Sniper запущено!</b>\n\n"
            + format_auto_status(),
            parse_mode=ParseMode.HTML,
        )

    # ── Manual snipe: pick side ───────────────────────────
    elif data.startswith("snipe_side:"):
        side = data.split(":")[1]  # "YES" or "NO"
        setup["side"] = side
        setup["step"] = "pick_price"

        buttons = [
            [InlineKeyboardButton("80¢", callback_data="snipe_price:80"),
             InlineKeyboardButton("85¢", callback_data="snipe_price:85")],
            [InlineKeyboardButton("88¢", callback_data="snipe_price:88"),
             InlineKeyboardButton("90¢", callback_data="snipe_price:90")],
            [InlineKeyboardButton("92¢", callback_data="snipe_price:92"),
             InlineKeyboardButton("95¢", callback_data="snipe_price:95")],
        ]
        await query.edit_message_text(
            f"{'🟢' if side == 'YES' else '🔴'} {side}\n\n"
            f"Ціна входу (лімітка):\n"
            f"Чим вища — тим частіше fill, але менше профіту.\n"
            f"80¢ = рідко fill, +20¢ profit\n"
            f"92¢ = часто fill, +8¢ profit",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Pick price ────────────────────────────────────────
    elif data.startswith("snipe_price:"):
        price_cents = int(data.split(":")[1])
        setup["price"] = price_cents / 100
        setup["step"] = "pick_size"

        buttons = [
            [InlineKeyboardButton("$0.50", callback_data="snipe_size:0.5"),
             InlineKeyboardButton("$1", callback_data="snipe_size:1")],
            [InlineKeyboardButton("$2", callback_data="snipe_size:2"),
             InlineKeyboardButton("$5", callback_data="snipe_size:5")],
        ]
        await query.edit_message_text(
            f"{'🟢' if setup['side'] == 'YES' else '🔴'} {setup['side']} @ {price_cents}¢\n\n"
            f"Розмір ордера:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Pick size ─────────────────────────────────────────
    elif data.startswith("snipe_size:"):
        size = float(data.split(":")[1])
        setup["size"] = size
        setup["step"] = "pick_roll"

        buttons = [
            [InlineKeyboardButton("🔄 Auto-roll ON", callback_data="snipe_roll:yes"),
             InlineKeyboardButton("1️⃣ Один раз", callback_data="snipe_roll:no")],
        ]
        await query.edit_message_text(
            f"{'🟢' if setup['side'] == 'YES' else '🔴'} {setup['side']} @ {int(setup['price']*100)}¢ | ${size:.2f}\n\n"
            f"Авто-ролл? (автоматично переходити на наступний ринок після резолюції)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Pick auto-roll → confirm ──────────────────────────
    elif data.startswith("snipe_roll:"):
        auto_roll = data.split(":")[1] == "yes"
        setup["auto_roll"] = auto_roll

        side = setup["side"]
        price = setup["price"]
        size = setup["size"]
        mtype = setup.get("market_type", "15m")
        title = setup["market"].get("question", "?")
        shares = round(size / price, 2)
        profit_if_win = round(shares * (1 - price), 2)
        loss_if_lose = round(size, 2)

        book = setup.get("book")
        book_text = ""
        if book:
            book_text = f"\n📖 Mid: {book['mid']*100:.0f}¢ | Spread: {book['spread']*100:.0f}¢"

        buttons = [
            [InlineKeyboardButton("🎯 START SNIPER", callback_data="snipe_go:yes"),
             InlineKeyboardButton("❌ Cancel", callback_data="snipe_go:no")],
        ]
        await query.edit_message_text(
            f"🎯 <b>Sniper — Confirm</b>\n\n"
            f"📌 {title[:70]}\n"
            f"{'🟢' if side == 'YES' else '🔴'} {side} @ {int(price*100)}¢\n"
            f"💵 ${size:.2f} = {shares:.1f} shares\n"
            f"✅ Win: +${profit_if_win:.2f} ({int((1-price)*100)}¢/share)\n"
            f"❌ Loss: -${loss_if_lose:.2f}\n"
            f"🔄 Auto-roll: {'ON' if auto_roll else 'OFF'} ({mtype})"
            f"{book_text}\n\n"
            f"Запускаємо?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    # ── Confirm → GO! ─────────────────────────────────────
    elif data.startswith("snipe_go:"):
        choice = data.split(":")[1]
        if choice == "no":
            _snipe_setup.pop(uid, None)
            await query.edit_message_text("❌ Скасовано.")
            return

        try:
            from sniper import start_session, format_session_status, get_token_id
        except (ImportError, ModuleNotFoundError):
            pass  # module removed in v2

        side = setup["side"]
        price = setup["price"]
        size = setup["size"]
        cid = setup["cid"]
        slug = setup["slug"]
        market = setup["market"]
        event = setup["event"]
        auto_roll = setup.get("auto_roll", False)
        mtype = setup.get("market_type", "15m")

        # Get correct token ID
        outcome_for_api = "yes" if side == "YES" else "no"
        token_id = get_token_id(cid, outcome_for_api)

        if not token_id:
            await query.edit_message_text("❌ Не вдалось знайти token_id.")
            _snipe_setup.pop(uid, None)
            return

        session = start_session(
            condition_id=cid,
            token_id=token_id,
            outcome=side,
            title=market.get("question", "?"),
            event_slug=slug,
            entry_price=price,
            size_usdc=size,
            side=side,
            auto_roll=auto_roll,
            market_type=mtype,
        )

        _snipe_setup.pop(uid, None)

        if session:
            await query.edit_message_text(
                format_session_status(session),
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.edit_message_text("❌ Не вдалось розмістити ордер. Перевір баланс.")


# ── Adaptive BTC Bot Commands ────────────────────────────────

@owner_only
async def mm_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mm_bot [status|stop]"""
    try:
        from btc_mm import start_mm, stop_mm, is_mm_active, get_mm_status
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    args = context.args or []
    action = args[0].lower() if args else ""

    if action == "status":
        await update.message.reply_text(get_mm_status(), parse_mode=ParseMode.HTML)
        return

    if action == "stop":
        if not is_mm_active():
            await update.message.reply_text("🔄 MM Bot вже вимкнений.")
            return
        stop_mm()
        await update.message.reply_text("🛑 MM Bot зупинено. Всі ордери скасовані.")
        return

    if is_mm_active():
        await update.message.reply_text(get_mm_status(), parse_mode=ParseMode.HTML)
        return

    start_mm()
    await update.message.reply_text(
        "🔄 <b>Market Maker Bot запущено!</b>\n\n"
        "Стратегія:\n"
        "1️⃣ Чекаємо flat + volatile ринок (немає тренду, є коливання)\n"
        "2️⃣ Купуємо YES 50¢ + NO 50¢\n"
        "3️⃣ Ставимо sell лімітки 60¢ на обидва\n"
        "4️⃣ Один продається → +10¢\n"
        "5️⃣ Другий → stop loss 40¢ (max -10¢)\n\n"
        "⚙️ Entry: тільки коли BTC < 0.04% change + volatility > $15\n"
        "🛡 Emergency close за 60с до кінця\n\n"
        "<code>/mm_bot status</code> — статус\n"
        "<code>/mm_bot stop</code> — зупинити",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def liq_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/liq_bot [status|stop]"""
    try:
        from btc_liquidity import start_liq, stop_liq, is_liq_active, get_liq_status
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    args = context.args or []
    action = args[0].lower() if args else ""

    if action == "status":
        await update.message.reply_text(get_liq_status(), parse_mode=ParseMode.HTML)
        return

    if action == "stop":
        if not is_liq_active():
            await update.message.reply_text("📊 Liq Bot вже вимкнений.")
            return
        stop_liq()
        await update.message.reply_text("🛑 Liquidity Scalper зупинено.")
        return

    if is_liq_active():
        await update.message.reply_text(get_liq_status(), parse_mode=ParseMode.HTML)
        return

    start_liq()
    await update.message.reply_text(
        "📊 <b>Liquidity Scalper запущено!</b>\n\n"
        "Стратегія:\n"
        "1️⃣ Сканую orderbook кожні 3с\n"
        "2️⃣ Знаходжу великі bid walls (підтримка)\n"
        "3️⃣ Лімітка на buy трохи вище стіни\n"
        "4️⃣ Лімітка на sell +8-10¢ вище\n"
        "5️⃣ Stop loss лімітка нижче стіни\n\n"
        "💰 <b>Всі ордери — лімітки (0% комісія)</b>\n\n"
        "<code>/liq_bot status</code> — статус\n"
        "<code>/liq_bot stop</code> — зупинити",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def weather_trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/weather_trade [status|stop]"""
    try:
        from weather_trader import start_weather, stop_weather, is_weather_active, get_weather_status
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    args = context.args or []
    action = args[0].lower() if args else ""

    if action == "status":
        await update.message.reply_text(get_weather_status(), parse_mode=ParseMode.HTML)
        return

    if action == "stop":
        if not is_weather_active():
            await update.message.reply_text("🌤 Weather Trader вже вимкнений.")
            return
        stop_weather()
        await update.message.reply_text("🛑 Weather Trader зупинено.")
        return

    if is_weather_active():
        await update.message.reply_text(get_weather_status(), parse_mode=ParseMode.HTML)
        return

    start_weather()
    await update.message.reply_text(
        "🌤 <b>Weather Trader запущено!</b>\n\n"
        "Стратегія:\n"
        "1️⃣ 3 Weather APIs (Open-Meteo, OWM, WeatherAPI)\n"
        "2️⃣ Консенсус 2/3 = high confidence\n"
        "3️⃣ Купую найбільш вірогідний outcome по лімітці\n"
        "4️⃣ Кожні 15с перевіряю прогноз\n"
        "5️⃣ Прогноз змінився → продаю + купую нову позицію\n\n"
        "🏙 Ринки: London\n"
        "📡 Оновлення: кожні 15 секунд\n\n"
        "<code>/weather_trade status</code> — статус\n"
        "<code>/weather_trade stop</code> — зупинити",
        parse_mode=ParseMode.HTML,
    )


# ── Adaptive BTC Bot Commands ────────────────────────────────

@owner_only
async def adaptive_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/15min_bot [status|stop]"""
    try:
        from btc_adaptive import start_adaptive, stop_adaptive, is_active, get_status
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    args = context.args or []
    action = args[0].lower() if args else ""

    if action == "status":
        await update.message.reply_text(get_status(), parse_mode=ParseMode.HTML)
        return

    if action == "stop":
        if not is_active():
            await update.message.reply_text("🤖 Adaptive Bot вже вимкнений.")
            return
        stop_adaptive()
        await update.message.reply_text("🛑 Adaptive BTC Bot зупинено.")
        return

    # Start
    if is_active():
        await update.message.reply_text(get_status(), parse_mode=ParseMode.HTML)
        return

    start_adaptive()
    await update.message.reply_text(
        "🤖 <b>Adaptive BTC Bot запущено!</b>\n\n"
        "Бот сам аналізує кожен 15-хвилинний ринок і вибирає:\n"
        "🟢 <b>CONFIDENT</b> — сильний тренд → 88¢, останні 45с\n"
        "🟡 <b>MODERATE</b> — помірний тренд → 70¢, останні 90с\n"
        "🔵 <b>EARLY</b> — ранній сигнал → 58¢, останні 150с\n\n"
        "📊 Моніторю 24/7. Кожний трейд → Telegram + Google Sheets.\n\n"
        "<code>/15min_bot status</code> — подивитись статистику\n"
        "<code>/15min_bot stop</code> — зупинити",
        parse_mode=ParseMode.HTML,
    )


# ── Weather Sniper Commands ──────────────────────────────────

@owner_only
async def weather_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start weather sniper: /weather <polymarket_url> [max_price_cents] [size_usd]"""
    try:
        from weather_sniper import start_weather_sniper, parse_polymarket_url
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🌤 <b>Weather Sniper</b>\n\n"
            "Використання:\n"
            "<code>/weather URL [ціна] [розмір] [годин_до]</code>\n\n"
            "Приклад:\n"
            "<code>/weather https://polymarket.com/event/highest-temperature-in-london-on-february-20 65 2 10</code>\n\n"
            "• Ціна — макс лімітка (за замовч. 65¢)\n"
            "• Розмір — $ на outcome (за замовч. $2)\n"
            "• Годин до — за скільки годин входити (за замовч. 10)",
            parse_mode=ParseMode.HTML,
        )
        return

    url = args[0]
    max_price = int(args[1]) / 100 if len(args) > 1 else 0.65
    size = float(args[2]) if len(args) > 2 else 2.0
    hours_before = float(args[3]) if len(args) > 3 else 10

    parsed = parse_polymarket_url(url)
    if not parsed:
        await update.message.reply_text("❌ Невірна силка Polymarket.")
        return

    await update.message.reply_text("🔍 Завантажую ринок...")

    sniper = start_weather_sniper(url, max_price, size, hours_before)
    if not sniper:
        await update.message.reply_text("❌ Не вдалось знайти ринок. Перевір силку.")
        return

    outcome_lines = []
    for o in sniper.outcomes:
        prob = f"{o.market_prob*100:.0f}%" if o.market_prob else "?"
        outcome_lines.append(f"  • {o.outcome_name[:40]} — {prob}")

    # Calculate hours left
    now = int(time.time())
    hours_left = (sniper.event_end_ts - now) / 3600 if sniper.event_end_ts > 0 else -1
    
    if hours_left > 0:
        timing_text = f"⏱ Закриття через {hours_left:.1f}h | Вхід за {hours_before:.0f}h до кінця"
    else:
        timing_text = "⚠️ Не вдалось визначити час закриття"

    await update.message.reply_text(
        f"🌤 <b>Weather Sniper запущено!</b>\n\n"
        f"📌 {sniper.event_title[:60]}\n"
        f"💰 ${size:.0f}/outcome | Max: {max_price*100:.0f}¢\n"
        f"{timing_text}\n"
        f"📊 Outcomes ({len(sniper.outcomes)}):\n"
        + "\n".join(outcome_lines)
        + "\n\n⏳ Моніторю, ставлю лімітку на лідера коли прийде час...",
        parse_mode=ParseMode.HTML,
    )


@owner_only
async def weather_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show weather sniper status."""
    try:
        from weather_sniper import format_weather_status
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2
    await update.message.reply_text(format_weather_status(), parse_mode=ParseMode.HTML)


@owner_only
async def weather_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop weather snipers."""
    try:
        from weather_sniper import stop_all_weather, get_all_weather_snipers
    except (ImportError, ModuleNotFoundError):
        pass  # module removed in v2

    args = context.args or []
    if args:
        # Stop specific by slug
        try:
            from weather_sniper import stop_weather_sniper
        except (ImportError, ModuleNotFoundError):
            pass  # module removed in v2
        s = stop_weather_sniper(args[0])
        if s:
            await update.message.reply_text(f"🛑 Stopped: {s.event_title[:50]}")
        else:
            await update.message.reply_text("❌ Не знайдено.")
        return

    stopped = stop_all_weather()
    if not stopped:
        await update.message.reply_text("🌤 Немає активних weather snipers.")
        return
    await update.message.reply_text(f"🛑 Зупинено {len(stopped)} weather sniper(s).")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("nick", nick_cmd))
    app.add_handler(CommandHandler("autocopy", autocopy_cmd))
    app.add_handler(CommandHandler("events", events_cmd))
    app.add_handler(CommandHandler("snipe90", snipe90_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("cleanup", cleanup_cmd))
    app.add_handler(CommandHandler("reset_pnl", reset_pnl_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("snipe", snipe_cmd))
    app.add_handler(CommandHandler("snipe_auto", snipe_auto_cmd))
    app.add_handler(CommandHandler("snipe_status", snipe_status_cmd))
    app.add_handler(CommandHandler("snipe_stop", snipe_stop_cmd))
    app.add_handler(CallbackQueryHandler(snipe_callback_handler, pattern=r"^snipe_"))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("weather_status", weather_status_cmd))
    app.add_handler(CommandHandler("weather_stop", weather_stop_cmd))
    app.add_handler(CommandHandler("15min_bot", adaptive_bot_cmd))
    app.add_handler(CommandHandler("mm_bot", mm_bot_cmd))
    app.add_handler(CommandHandler("liq_bot", liq_bot_cmd))
    app.add_handler(CommandHandler("weather_trade", weather_trade_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, custom_amount_handler))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
