import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Telegram ─────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "535860827"))

# ── Polymarket APIs ──────────────────────────────────────────────
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# ── Wallet ───────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "")
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

# ── Polling ──────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "3"))

# ── Database ─────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "tracker.db")

# ── Copy Trading ─────────────────────────────────────────────────
COPY_RATIO = float(os.getenv("COPY_RATIO", "0.10"))       # 10% of trader's amount
MIN_COPY_AMOUNT = float(os.getenv("MIN_COPY_AMOUNT", "1.0"))  # min $1 per trade
MAX_COPY_AMOUNT = float(os.getenv("MAX_COPY_AMOUNT", "15.0")) # max $15 per trade
BUY_SLIPPAGE = float(os.getenv("BUY_SLIPPAGE", "0.015"))  # +1.5¢ above trader price
SAFETY_BUFFER = float(os.getenv("SAFETY_BUFFER", "0.50"))   # keep $0.50 reserve

# ── Channel ──────────────────────────────────────────────────────
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
