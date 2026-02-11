"""
Google Sheets integration — syncs trader data to a Google Spreadsheet.
Each trader gets their own sheet tab.
Updates every 5 minutes.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_gc = None  # gspread client
_spreadsheet = None


def _get_client():
    """Lazy-init Google Sheets client."""
    global _gc, _spreadsheet
    if _gc is not None:
        return _gc, _spreadsheet

    try:
        import gspread
        from config import GOOGLE_SHEET_ID, GOOGLE_CREDS_FILE
        import os
        import json

        # Option 1: JSON string in env variable (for Railway/Docker)
        creds_json = os.getenv("GOOGLE_CREDS_JSON", "")
        if creds_json:
            creds_dict = json.loads(creds_json)
            _gc = gspread.service_account_from_dict(creds_dict)
        else:
            # Option 2: credentials.json file (local)
            creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), GOOGLE_CREDS_FILE)
            if not os.path.exists(creds_path):
                creds_path = GOOGLE_CREDS_FILE
            logger.info("Looking for credentials at: %s (exists: %s)", creds_path, os.path.exists(creds_path))
            _gc = gspread.service_account(filename=creds_path)

        _spreadsheet = _gc.open_by_key(GOOGLE_SHEET_ID)
        logger.info("Google Sheets connected: %s", _spreadsheet.title)
        return _gc, _spreadsheet
    except FileNotFoundError:
        logger.warning("credentials.json not found — Google Sheets disabled")
    except Exception as e:
        logger.error("Google Sheets init error: %s — %s", type(e).__name__, e)
    _gc = False  # Mark as failed so we don't retry
    return None, None


def _get_or_create_sheet(spreadsheet, title: str):
    """Get existing worksheet or create new one."""
    try:
        return spreadsheet.worksheet(title)
    except Exception:
        return spreadsheet.add_worksheet(title=title, rows=100, cols=20)


def _ts_to_str(ts):
    """Timestamp to human readable string."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _safe_float(v, default=0.0):
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return default


async def update_sheets():
    """Main update function — called every 5 min from the poller."""
    from database import (
        get_all_traders, get_display_name, get_closed_trades,
        get_open_positions, get_all_trades_with_hashtag,
        get_all_open_copy_trades, get_closed_copy_trades, get_copy_trades_by_hashtag,
    )

    gc, spreadsheet = _get_client()
    if not gc or not spreadsheet:
        return

    try:
        traders = get_all_traders()

        # ── Per-trader sheets ──
        for trader in traders:
            name = get_display_name(trader)
            address = trader["address"]

            # Sheet name max 100 chars, sanitize
            sheet_name = name[:90].replace("/", "-")

            try:
                ws = _get_or_create_sheet(spreadsheet, sheet_name)
                await _update_trader_sheet(ws, trader, address, name)
            except Exception as e:
                logger.error("Sheet update error for %s: %s", name, e)

        # ── My Copies sheet ──
        try:
            ws = _get_or_create_sheet(spreadsheet, "📊 My Copies")
            await _update_copies_sheet(ws)
        except Exception as e:
            logger.error("Copies sheet error: %s", e)

        # ── P&L by Hashtag sheet ──
        try:
            ws = _get_or_create_sheet(spreadsheet, "📈 P&L by Hashtag")
            await _update_hashtag_sheet(ws)
        except Exception as e:
            logger.error("Hashtag sheet error: %s", e)

        logger.info("Google Sheets updated (%d traders)", len(traders))

    except Exception as e:
        logger.error("Sheets update error: %s", e)


async def _update_trader_sheet(ws, trader: dict, address: str, name: str):
    """Update a single trader's sheet with open positions and closed trades."""
    from database import get_closed_trades, get_open_positions, get_all_trades_with_hashtag

    # Get current prices for open positions
    open_pos = get_open_positions(address)
    closed = get_closed_trades(address, limit=20)

    rows = []

    # ── Header ──
    rows.append([f"📊 {name}", "", "", "", "", "", "", ""])
    rows.append([f"Address: {address[:10]}...{address[-6:]}", "", "", "",
                 f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "", "", ""])
    rows.append([""])

    # ── Open Positions ──
    rows.append(["🟢 OPEN POSITIONS", "", "", "", "", "", "", ""])
    rows.append(["Market", "Outcome", "Entry Price", "Shares", "Invested", "Current Price", "P&L $", "Hashtag"])

    if open_pos:
        for pos in open_pos:
            cur_price = await _get_current_price(pos.get("token_id", ""))
            entry = _safe_float(pos.get("buy_price"))
            shares = _safe_float(pos.get("size"))
            invested = _safe_float(pos.get("usdc_size"))
            cur_val = shares * cur_price if cur_price else 0
            unrealized = cur_val - invested if cur_price else 0

            rows.append([
                pos.get("title", "?")[:60],
                pos.get("outcome", "?"),
                f"{entry:.2f}",
                f"{shares:.1f}",
                f"${invested:.2f}",
                f"{cur_price:.2f}" if cur_price else "?",
                f"${unrealized:+.2f}" if cur_price else "?",
                pos.get("hashtag", ""),
            ])
    else:
        rows.append(["No open positions", "", "", "", "", "", "", ""])

    rows.append([""])

    # ── Closed Trades (last 20) ──
    rows.append(["🔴 LAST 20 CLOSED TRADES", "", "", "", "", "", "", ""])
    rows.append(["Market", "Outcome", "Entry", "Exit", "Invested", "P&L $", "P&L %", "Hashtag"])

    for t in closed:
        rows.append([
            t.get("title", "?")[:60],
            t.get("outcome", "?"),
            f"{_safe_float(t.get('buy_price')):.2f}",
            f"{_safe_float(t.get('sell_price')):.2f}",
            f"${_safe_float(t.get('usdc_size')):.2f}",
            f"${_safe_float(t.get('pnl_usdc')):+.2f}",
            f"{_safe_float(t.get('pnl_pct')):+.1f}%",
            t.get("hashtag", ""),
        ])

    if not closed:
        rows.append(["No closed trades yet", "", "", "", "", "", "", ""])

    rows.append([""])

    # ── P&L by Hashtag ──
    hashtag_trades = get_all_trades_with_hashtag(address)
    if hashtag_trades:
        rows.append(["📈 P&L BY HASHTAG", "", "", "", "", "", "", ""])
        rows.append(["Hashtag", "Trades", "Wins", "Win%", "Total P&L", "", "", ""])

        # Aggregate
        ht_agg = {}
        for ht in hashtag_trades:
            tag = ht.get("hashtag", "#інше")
            if tag not in ht_agg:
                ht_agg[tag] = {"count": 0, "wins": 0, "pnl": 0.0}
            ht_agg[tag]["count"] += 1
            pnl = _safe_float(ht.get("pnl_usdc"))
            ht_agg[tag]["pnl"] += pnl
            if pnl > 0:
                ht_agg[tag]["wins"] += 1

        for tag, data in sorted(ht_agg.items(), key=lambda x: x[1]["pnl"], reverse=True):
            winrate = (data["wins"] / data["count"] * 100) if data["count"] > 0 else 0
            rows.append([
                tag,
                str(data["count"]),
                str(data["wins"]),
                f"{winrate:.0f}%",
                f"${data['pnl']:+.2f}",
                "", "", "",
            ])

    # Write all at once (batch update)
    ws.clear()
    if rows:
        ws.update(range_name=f"A1:H{len(rows)}", values=rows)


async def _update_copies_sheet(ws):
    """Update the My Copies sheet with open and closed copy trades."""
    from database import get_all_open_copy_trades, get_closed_copy_trades, get_all_traders, get_display_name

    traders = {t["address"]: get_display_name(t) for t in get_all_traders()}
    open_copies = get_all_open_copy_trades()
    closed_copies = get_closed_copy_trades(limit=30)

    rows = []
    rows.append(["💰 MY COPY TRADES", "", "", "", "", "", "", "", ""])
    rows.append([f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "", "", "", "", "", "", "", ""])
    rows.append([""])

    # Open
    rows.append(["🟢 OPEN COPIES", "", "", "", "", "", "", "", ""])
    rows.append(["Trader", "Market", "Outcome", "Entry", "Shares", "Invested", "Current", "P&L $", "Hashtag"])

    for c in open_copies:
        cur_price = await _get_current_price(c.get("token_id", ""))
        invested = _safe_float(c.get("usdc_spent"))
        shares = _safe_float(c.get("shares"))
        cur_val = shares * cur_price if cur_price else 0
        unrealized = cur_val - invested if cur_price else 0
        trader_name = traders.get(c.get("trader_address", ""), "?")

        rows.append([
            trader_name,
            c.get("title", "?")[:50],
            c.get("outcome", "?"),
            f"{_safe_float(c.get('buy_price')):.2f}",
            f"{shares:.1f}",
            f"${invested:.2f}",
            f"{cur_price:.2f}" if cur_price else "?",
            f"${unrealized:+.2f}" if cur_price else "?",
            c.get("hashtag", ""),
        ])

    if not open_copies:
        rows.append(["No open copy trades", "", "", "", "", "", "", "", ""])

    rows.append([""])

    # Closed
    rows.append(["🔴 CLOSED COPIES (Last 30)", "", "", "", "", "", "", "", ""])
    rows.append(["Trader", "Market", "Outcome", "Entry", "Exit", "Invested", "P&L $", "P&L %", "Hashtag"])

    for c in closed_copies:
        trader_name = traders.get(c.get("trader_address", ""), "?")
        rows.append([
            trader_name,
            c.get("title", "?")[:50],
            c.get("outcome", "?"),
            f"{_safe_float(c.get('buy_price')):.2f}",
            f"{_safe_float(c.get('sell_price')):.2f}",
            f"${_safe_float(c.get('usdc_spent')):.2f}",
            f"${_safe_float(c.get('pnl_usdc')):+.2f}",
            f"{_safe_float(c.get('pnl_pct')):+.1f}%",
            c.get("hashtag", ""),
        ])

    if not closed_copies:
        rows.append(["No closed copy trades yet", "", "", "", "", "", "", "", ""])

    ws.clear()
    if rows:
        ws.update(range_name=f"A1:I{len(rows)}", values=rows)


async def _update_hashtag_sheet(ws):
    """P&L breakdown by hashtag across all traders."""
    from database import get_all_traders, get_display_name, get_all_trades_with_hashtag, get_copy_trades_by_hashtag

    rows = []
    rows.append(["📈 P&L BY HASHTAG", "", "", "", "", ""])
    rows.append([f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "", "", "", "", ""])
    rows.append([""])

    # Per trader hashtag stats
    traders = get_all_traders()
    for trader in traders:
        name = get_display_name(trader)
        address = trader["address"]
        ht_trades = get_all_trades_with_hashtag(address)

        if not ht_trades:
            continue

        rows.append([f"👤 {name}", "", "", "", "", ""])
        rows.append(["Hashtag", "Trades", "Wins", "Win%", "Total P&L", ""])

        ht_agg = {}
        for ht in ht_trades:
            tag = ht.get("hashtag", "#інше")
            if tag not in ht_agg:
                ht_agg[tag] = {"count": 0, "wins": 0, "pnl": 0.0}
            ht_agg[tag]["count"] += 1
            pnl = _safe_float(ht.get("pnl_usdc"))
            ht_agg[tag]["pnl"] += pnl
            if pnl > 0:
                ht_agg[tag]["wins"] += 1

        for tag, data in sorted(ht_agg.items(), key=lambda x: x[1]["pnl"], reverse=True):
            winrate = (data["wins"] / data["count"] * 100) if data["count"] > 0 else 0
            rows.append([tag, str(data["count"]), str(data["wins"]), f"{winrate:.0f}%", f"${data['pnl']:+.2f}", ""])

        rows.append([""])

    # My copy trades hashtag stats
    copy_ht = get_copy_trades_by_hashtag()
    if copy_ht:
        rows.append(["💰 MY COPY TRADES", "", "", "", "", ""])
        rows.append(["Hashtag", "Trades", "Wins", "Win%", "Total P&L", "Total Invested"])
        for ch in copy_ht:
            total = ch.get("total", 0)
            wins = ch.get("wins", 0)
            winrate = (wins / total * 100) if total > 0 else 0
            rows.append([
                ch.get("hashtag", "#інше"),
                str(total),
                str(wins),
                f"{winrate:.0f}%",
                f"${_safe_float(ch.get('total_pnl')):+.2f}",
                f"${_safe_float(ch.get('total_invested')):.2f}",
            ])

    ws.clear()
    if rows:
        ws.update(range_name=f"A1:F{len(rows)}", values=rows)


async def _get_current_price(token_id: str) -> float | None:
    """Get current midpoint price for a token from CLOB API."""
    if not token_id:
        return None
    try:
        import aiohttp
        url = f"https://clob.polymarket.com/midpoint?token_id={token_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    mid = data.get("mid")
                    if mid:
                        return float(mid)
    except Exception:
        pass
    return None


async def sheets_updater():
    """Background task that updates Google Sheets every 5 minutes."""
    from config import SHEETS_UPDATE_INTERVAL

    gc, _ = _get_client()
    if not gc:
        logger.info("Google Sheets disabled (no credentials)")
        return

    logger.info("Sheets updater started (interval=%ds)", SHEETS_UPDATE_INTERVAL)

    # Wait 30s after boot before first update
    await asyncio.sleep(30)

    while True:
        try:
            await update_sheets()
        except Exception as e:
            logger.error("Sheets updater error: %s", e)
        await asyncio.sleep(SHEETS_UPDATE_INTERVAL)