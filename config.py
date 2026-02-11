import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, use system env vars

# ── Telegram ─────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "7973877821:AAHajfFQP8_z0w6zBgla7er8oJJN6r2lnVo")
OWNER_ID = int(os.getenv("OWNER_ID", "535860827"))

# ── Polymarket APIs ──────────────────────────────────────────────
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# ── Your wallet (MetaMask) ───────────────────────────────────────
# Private key exported from MetaMask (DO NOT SHARE)
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
# Your Polymarket proxy wallet address (shown in polymarket.com/settings)
FUNDER_ADDRESS = os.getenv("FUNDER_ADDRESS", "0xE433Db358AeD293216F77124E800dC97977Cdb89")
# MetaMask = signature_type 2
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "2"))

# ── Polling ──────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "15"))  # 15 seconds

# ── Database ─────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "tracker.db")

# ── Google Sheets ────────────────────────────────────────────────
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1hxflPVVUCI7QbFD6KOQXgtVKoVSFwhEDbl_yqR03Wqw")
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "credentials.json")
SHEETS_UPDATE_INTERVAL = int(os.getenv("SHEETS_UPDATE_INTERVAL", "300"))  # 5 min

# ── Copy Trades Channel ─────────────────────────────────────────
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003565414340"))