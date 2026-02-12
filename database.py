import sqlite3
import time
from config import DB_PATH


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS traders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT UNIQUE NOT NULL,
            username TEXT,
            nickname TEXT,
            profile_url TEXT,
            autocopy INTEGER DEFAULT 0,
            added_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trader_address TEXT NOT NULL,
            transaction_hash TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            UNIQUE(trader_address, transaction_hash)
        );
        CREATE TABLE IF NOT EXISTS buy_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trader_address TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            buy_price REAL NOT NULL,
            usdc_size REAL NOT NULL,
            size REAL NOT NULL,
            message_id INTEGER NOT NULL,
            timestamp INTEGER NOT NULL,
            title TEXT,
            token_id TEXT,
            hashtag TEXT,
            closed INTEGER DEFAULT 0,
            sell_price REAL,
            sell_usdc REAL,
            sell_timestamp INTEGER,
            pnl_usdc REAL,
            pnl_pct REAL
        );
        CREATE TABLE IF NOT EXISTS copy_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trader_address TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            buy_price REAL NOT NULL,
            usdc_spent REAL NOT NULL,
            shares REAL NOT NULL,
            order_id TEXT,
            timestamp INTEGER NOT NULL,
            title TEXT,
            hashtag TEXT,
            sell_price REAL,
            sell_usdc REAL,
            sell_timestamp INTEGER,
            pnl_usdc REAL,
            pnl_pct REAL,
            status TEXT DEFAULT 'OPEN',
            source TEXT DEFAULT 'manual'
        );
        CREATE TABLE IF NOT EXISTS autocopy_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trader_address TEXT NOT NULL,
            date TEXT NOT NULL,
            big_trade_count INTEGER DEFAULT 0,
            UNIQUE(trader_address, date)
        );
        CREATE INDEX IF NOT EXISTS idx_buy_lookup
            ON buy_messages(trader_address, condition_id, outcome, closed);
        CREATE INDEX IF NOT EXISTS idx_copy_lookup
            ON copy_trades(trader_address, condition_id, outcome, status);
    """)
    _migrate(conn)
    conn.commit()
    conn.close()


def _migrate(conn):
    """Add new columns to existing tables if they don't exist."""
    existing = {}
    for table in ["traders", "buy_messages", "copy_trades"]:
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing[table] = {c["name"] for c in cols}

    migrations = [
        ("traders", "nickname", "TEXT"),
        ("traders", "autocopy", "INTEGER DEFAULT 0"),
        ("traders", "autocopy_tags", "TEXT"),
        ("buy_messages", "hashtag", "TEXT"),
        ("buy_messages", "sell_price", "REAL"),
        ("buy_messages", "sell_usdc", "REAL"),
        ("buy_messages", "sell_timestamp", "INTEGER"),
        ("buy_messages", "pnl_usdc", "REAL"),
        ("buy_messages", "pnl_pct", "REAL"),
        ("copy_trades", "hashtag", "TEXT"),
        ("copy_trades", "pnl_usdc", "REAL"),
        ("copy_trades", "pnl_pct", "REAL"),
        ("copy_trades", "source", "TEXT DEFAULT 'manual'"),
    ]
    for table, col, coltype in migrations:
        if col not in existing.get(table, set()):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass


# ── Traders ──────────────────────────────────────────────────────

def add_trader(address: str, username: str | None = None, profile_url: str | None = None) -> bool:
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO traders (address, username, profile_url, added_at) VALUES (?, ?, ?, ?)",
            (address.lower(), username, profile_url, int(time.time()))
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_trader(address: str, username: str | None = None, profile_url: str | None = None):
    conn = get_db()
    fields, values = [], []
    if username is not None:
        fields.append("username = ?"); values.append(username)
    if profile_url is not None:
        fields.append("profile_url = ?"); values.append(profile_url)
    if fields:
        values.append(address.lower())
        conn.execute(f"UPDATE traders SET {', '.join(fields)} WHERE address = ?", values)
        conn.commit()
    conn.close()


def set_nickname(address: str, nickname: str) -> bool:
    conn = get_db()
    cursor = conn.execute("UPDATE traders SET nickname = ? WHERE address = ?", (nickname, address.lower()))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def get_display_name(trader: dict) -> str:
    return trader.get("nickname") or trader.get("username") or trader.get("address", "?")[:10]


def remove_trader(address: str) -> bool:
    conn = get_db()
    cursor = conn.execute("DELETE FROM traders WHERE address = ?", (address.lower(),))
    conn.commit()
    removed = cursor.rowcount > 0
    if removed:
        conn.execute("DELETE FROM seen_trades WHERE trader_address = ?", (address.lower(),))
        conn.commit()
    conn.close()
    return removed


def get_all_traders() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT address, username, nickname, profile_url, autocopy, autocopy_tags, added_at FROM traders").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_trader_by_name(name: str) -> dict | None:
    traders = get_all_traders()
    name_lower = name.lower()
    for t in traders:
        if t.get("nickname") and t["nickname"].lower() == name_lower:
            return t
        if t.get("username") and t["username"].lower() == name_lower:
            return t
        if t["address"].lower().startswith(name_lower):
            return t
    return None


# ── Autocopy ─────────────────────────────────────────────────────

def set_autocopy(address: str, enabled: bool) -> bool:
    conn = get_db()
    cursor = conn.execute("UPDATE traders SET autocopy = ? WHERE address = ?", (1 if enabled else 0, address.lower()))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def set_autocopy_tags(address: str, tags: list[str]) -> bool:
    """Save allowed hashtags for autocopy. Empty list = all tags allowed."""
    import json
    conn = get_db()
    cursor = conn.execute("UPDATE traders SET autocopy_tags = ? WHERE address = ?",
                          (json.dumps(tags) if tags else None, address.lower()))
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def get_autocopy_tags(address: str) -> list[str]:
    """Get allowed hashtags for autocopy. Empty list = all allowed."""
    import json
    conn = get_db()
    row = conn.execute("SELECT autocopy_tags FROM traders WHERE address = ?", (address.lower(),)).fetchone()
    conn.close()
    if row and row["autocopy_tags"]:
        try:
            return json.loads(row["autocopy_tags"])
        except Exception:
            pass
    return []  # empty = all allowed


def get_autocopy_traders() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM traders WHERE autocopy = 1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_big_trade_count(address: str) -> int:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_db()
    row = conn.execute(
        "SELECT big_trade_count FROM autocopy_daily WHERE trader_address = ? AND date = ?",
        (address.lower(), today)
    ).fetchone()
    conn.close()
    return row["big_trade_count"] if row else 0


def increment_daily_big_trade(address: str):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = get_db()
    conn.execute(
        """INSERT INTO autocopy_daily (trader_address, date, big_trade_count)
           VALUES (?, ?, 1)
           ON CONFLICT(trader_address, date) DO UPDATE SET big_trade_count = big_trade_count + 1""",
        (address.lower(), today)
    )
    conn.commit()
    conn.close()


# ── Seen trades ──────────────────────────────────────────────────

def is_trade_seen(trader_address: str, tx_hash: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM seen_trades WHERE trader_address = ? AND transaction_hash = ?",
        (trader_address.lower(), tx_hash)
    ).fetchone()
    conn.close()
    return row is not None


def mark_trade_seen(trader_address: str, tx_hash: str, timestamp: int):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen_trades (trader_address, transaction_hash, timestamp) VALUES (?, ?, ?)",
            (trader_address.lower(), tx_hash, timestamp)
        )
        conn.commit()
    finally:
        conn.close()


def seed_existing_trades(trader_address: str, tx_hashes: list[tuple[str, int]]):
    conn = get_db()
    conn.executemany(
        "INSERT OR IGNORE INTO seen_trades (trader_address, transaction_hash, timestamp) VALUES (?, ?, ?)",
        [(trader_address.lower(), tx, ts) for tx, ts in tx_hashes]
    )
    conn.commit()
    conn.close()


# ── Buy message tracking ────────────────────────────────────────

def save_buy_message(
    trader_address: str, condition_id: str, outcome: str,
    buy_price: float, usdc_size: float, size: float,
    message_id: int, timestamp: int, title: str | None = None,
    token_id: str | None = None, hashtag: str | None = None,
):
    conn = get_db()
    conn.execute(
        """INSERT INTO buy_messages
           (trader_address, condition_id, outcome, buy_price, usdc_size, size,
            message_id, timestamp, title, token_id, hashtag)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trader_address.lower(), condition_id, outcome, buy_price, usdc_size,
         size, message_id, timestamp, title, token_id, hashtag)
    )
    conn.commit()
    conn.close()


def find_buy_message(trader_address: str, condition_id: str, outcome: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM buy_messages
           WHERE trader_address = ? AND condition_id = ? AND outcome = ? AND closed = 0
           ORDER BY timestamp DESC LIMIT 1""",
        (trader_address.lower(), condition_id, outcome)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def find_all_open_buys(trader_address: str, condition_id: str, outcome: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM buy_messages
           WHERE trader_address = ? AND condition_id = ? AND outcome = ? AND closed = 0
           ORDER BY timestamp ASC""",
        (trader_address.lower(), condition_id, outcome)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_buy_messages(trader_address: str, condition_id: str, outcome: str,
                       sell_price: float = 0, sell_usdc: float = 0,
                       pnl_usdc: float = 0, pnl_pct: float = 0):
    conn = get_db()
    conn.execute(
        """UPDATE buy_messages SET closed = 1, sell_price = ?, sell_usdc = ?,
           sell_timestamp = ?, pnl_usdc = ?, pnl_pct = ?
           WHERE trader_address = ? AND condition_id = ? AND outcome = ? AND closed = 0""",
        (sell_price, sell_usdc, int(time.time()), pnl_usdc, pnl_pct,
         trader_address.lower(), condition_id, outcome)
    )
    conn.commit()
    conn.close()


def get_closed_trades(trader_address: str, limit: int = 20) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM buy_messages WHERE trader_address = ? AND closed = 1
           ORDER BY sell_timestamp DESC LIMIT ?""",
        (trader_address.lower(), limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_positions(trader_address: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM buy_messages WHERE trader_address = ? AND closed = 0
           ORDER BY timestamp DESC""",
        (trader_address.lower(),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_trades_with_hashtag(trader_address: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT hashtag, pnl_usdc, pnl_pct, closed FROM buy_messages
           WHERE trader_address = ? AND closed = 1 AND hashtag IS NOT NULL
           ORDER BY sell_timestamp DESC""",
        (trader_address.lower(),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Copy trades ──────────────────────────────────────────────────

def save_copy_trade(
    trader_address: str, condition_id: str, token_id: str, outcome: str,
    buy_price: float, usdc_spent: float, shares: float,
    order_id: str | None, timestamp: int, title: str | None = None,
    hashtag: str | None = None, source: str = "manual",
    status: str = "OPEN",
) -> int:
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO copy_trades
           (trader_address, condition_id, token_id, outcome, buy_price, usdc_spent,
            shares, order_id, timestamp, title, hashtag, source, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trader_address.lower(), condition_id, token_id, outcome, buy_price,
         usdc_spent, shares, order_id, timestamp, title, hashtag, source, status)
    )
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id


def find_open_copy_trades(trader_address: str, condition_id: str, outcome: str) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM copy_trades
           WHERE trader_address = ? AND condition_id = ? AND outcome = ? AND status = 'OPEN'""",
        (trader_address.lower(), condition_id, outcome)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_pending_copy_trades(trader_address: str, condition_id: str, outcome: str) -> list[dict]:
    """Find PENDING copy trades for a specific market (not yet filled)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT * FROM copy_trades
           WHERE trader_address = ? AND condition_id = ? AND outcome = ? AND status = 'PENDING'""",
        (trader_address.lower(), condition_id, outcome)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_pending_copy_trades() -> list[dict]:
    """Get ALL pending copy trades across all traders."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM copy_trades WHERE status = 'PENDING'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_copy_trade_status(copy_id: int, status: str):
    """Update status: PENDING → OPEN, PENDING → CANCELLED, etc."""
    conn = get_db()
    conn.execute("UPDATE copy_trades SET status = ? WHERE id = ?", (status, copy_id))
    conn.commit()
    conn.close()


def close_copy_trade(copy_id: int, sell_price: float, sell_usdc: float, sell_timestamp: int,
                     pnl_usdc: float = 0, pnl_pct: float = 0):
    conn = get_db()
    conn.execute(
        """UPDATE copy_trades
           SET sell_price = ?, sell_usdc = ?, sell_timestamp = ?, status = 'CLOSED',
               pnl_usdc = ?, pnl_pct = ?
           WHERE id = ?""",
        (sell_price, sell_usdc, sell_timestamp, pnl_usdc, pnl_pct, copy_id)
    )
    conn.commit()
    conn.close()


def get_all_open_copy_trades() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM copy_trades WHERE status = 'OPEN'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_closed_copy_trades(limit: int = 50) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM copy_trades WHERE status = 'CLOSED' ORDER BY sell_timestamp DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_trader_sold(trader_address: str, condition_id: str, outcome: str) -> bool:
    """Check if trader already sold this market (buy_messages closed)."""
    conn = get_db()
    row = conn.execute(
        """SELECT COUNT(*) as cnt FROM buy_messages
           WHERE trader_address = ? AND condition_id = ? AND outcome = ? AND closed = 1""",
        (trader_address.lower(), condition_id, outcome)
    ).fetchone()
    conn.close()
    return row["cnt"] > 0 if row else False


def get_copy_trades_by_hashtag() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        """SELECT hashtag,
                  COUNT(*) as total,
                  SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
                  SUM(pnl_usdc) as total_pnl,
                  SUM(usdc_spent) as total_invested
           FROM copy_trades
           WHERE status = 'CLOSED' AND hashtag IS NOT NULL
           GROUP BY hashtag"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
