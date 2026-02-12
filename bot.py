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
    get_all_open_copy_trades,
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
    is_trading_enabled, get_balance, place_limit_buy,
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
        f"🔄 Polls every 15 sec\n"
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
        for c in copies:
            invested = float(c.get("usdc_spent", 0))
            total_invested += invested

            # Get current price
            token_id = c.get("token_id", "")
            cur_price = None
            if token_id:
                try:
                    async with aiohttp.ClientSession() as session:
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
        lines.append(f"\n<b>Відкриті позиції:</b>")
        lines.extend(position_lines)

    await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Callback handler ────────────────────────────────────────────
@owner_only
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

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

        result = place_limit_buy(token_id, price, amount, condition_id)

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
        BotCommand("nick", "✏️ Задати нікнейм"),
        BotCommand("list", "📋 Список трейдерів"),
        BotCommand("check", "🔍 Останні угоди"),
        BotCommand("balance", "💰 Баланс і P&L"),
        BotCommand("portfolio", "💼 Мої копі-трейди"),
        BotCommand("autocopy", "🤖 Автокопітрейдинг"),
    ])

    # Start poller
    asyncio.create_task(poll_traders(app.bot))
    logger.info("Poller task created")

    # Start Google Sheets updater
    try:
        from sheets import sheets_updater
        asyncio.create_task(sheets_updater())
        logger.info("Sheets updater task created")
    except Exception as e:
        logger.warning("Sheets updater failed to start: %s", e)

    # Start health monitor
    asyncio.create_task(health_monitor(app.bot))
    logger.info("Health monitor started")

    trading = "✅" if is_trading_enabled() else "❌ (no key)"
    try:
        await app.bot.send_message(
            chat_id=OWNER_ID,
            text=f"🤖 <b>Bot started!</b>\n⏱ Polling: 15s\n📊 Sheets: 5min\n🏥 Health: 5min\n💰 Trading: {trading}",
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


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("nick", nick_cmd))
    app.add_handler(CommandHandler("autocopy", autocopy_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, custom_amount_handler))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
