# Polymarket Copy-Trader Bot 🤖💰

Telegram bot that monitors Polymarket traders and lets you copy their trades with one tap.

## Features

- 🔍 **Track traders** by username or wallet address
- 🟢 **BUY notifications** with [Copy Trade] button
- 🔴 **SELL notifications** as reply to original BUY with P&L
- 💰 **One-tap copy trading** — choose amount, confirm, done
- 🤖 **Auto-sell** when tracked trader exits
- 📊 **P&L tracking** — profit/loss in $ and % on every exit
- ⚡ **30-second polling** — near real-time alerts

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Help + status |
| `/add @username` | Track a trader |
| `/remove name` | Stop tracking |
| `/list` | Watchlist with profile links |
| `/check` | Manual check latest trades |
| `/portfolio` | Your open copy-trades |

## Setup

### 1. Prerequisites

- Python 3.11+
- Telegram bot from @BotFather
- MetaMask wallet connected to Polymarket
- USDC on Polygon

### 2. Export MetaMask Private Key

1. Open MetaMask → Click your account
2. Account Details → Export Private Key
3. Enter password → Copy the key

⚠️ **NEVER share this key. Store only in `.env` on your server.**

### 3. Get your Polymarket Proxy Address

Go to polymarket.com/settings — copy your wallet address (0x...).
This is your FUNDER_ADDRESS.

### 4. Set Token Allowances (one time)

```bash
pip install web3
export PRIVATE_KEY=0x...your_key...
python set_allowances.py
```

This lets Polymarket contracts interact with your USDC. Only needed once.

### 5. Configure & Run

```bash
cp .env.example .env
# Edit .env with your values
pip install -r requirements.txt
python bot.py
```

### 6. Deploy (Railway)

1. Push to GitHub
2. railway.app → New Project → from GitHub
3. Add env vars: `BOT_TOKEN`, `OWNER_ID`, `PRIVATE_KEY`, `FUNDER_ADDRESS`
4. Deploy

## How Copy Trading Works

```
Trader buys Yes @ 55¢
       ↓
You get notification with [💰 Copy Trade] button
       ↓
Choose amount: $50 / $100 / $250 / custom
       ↓
Confirm → Bot places limit order at same price
       ↓
Trader sells → Bot auto-sells your position
       ↓
You get P&L notification
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram bot token |
| `OWNER_ID` | ✅ | Your Telegram user ID |
| `PRIVATE_KEY` | For trading | MetaMask private key |
| `FUNDER_ADDRESS` | For trading | Polymarket proxy wallet |
| `SIGNATURE_TYPE` | No | `2` for MetaMask (default) |
| `POLL_INTERVAL` | No | Seconds between checks (default: 30) |

## Security

- Private key stored ONLY in `.env` (never committed to git)
- Bot locked to your Telegram ID
- All orders are non-custodial (your key, your wallet)
- `.gitignore` excludes `.env` and database
