import re
import logging
import aiohttp
from config import DATA_API, GAMMA_API

logger = logging.getLogger(__name__)


def extract_address_or_username(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    match = re.search(r'polymarket\.com/@([^\s/?#]+)', url_or_id)
    if match:
        return match.group(1)
    match = re.search(r'polymarket\.com/profile/([^\s/?#]+)', url_or_id)
    if match:
        return match.group(1)
    if url_or_id.startswith("@"):
        url_or_id = url_or_id[1:]
    return url_or_id


async def resolve_username_to_address(session: aiohttp.ClientSession, username: str) -> str | None:
    username = username.lstrip("@")
    if username.startswith("0x") and len(username) == 42:
        return username.lower()

    try:
        async with session.get(f"{GAMMA_API}/public-search", params={"query": username}) as resp:
            if resp.status == 200:
                data = await resp.json()
                profiles = data.get("profiles", [])
                for p in profiles:
                    name = (p.get("name") or "").lower()
                    pseudonym = (p.get("pseudonym") or "").lower()
                    proxy = p.get("proxyWallet") or ""
                    if username.lower() in (name, pseudonym):
                        if proxy:
                            return proxy.lower()
                for p in profiles:
                    name = (p.get("name") or "").lower()
                    pseudonym = (p.get("pseudonym") or "").lower()
                    if username.lower() in name or username.lower() in pseudonym:
                        proxy = p.get("proxyWallet") or ""
                        if proxy:
                            return proxy.lower()
                if profiles:
                    proxy = profiles[0].get("proxyWallet") or ""
                    if proxy:
                        return proxy.lower()
    except Exception as e:
        logger.error(f"Search error for {username}: {e}")

    try:
        async with session.get(f"https://polymarket.com/@{username}", allow_redirects=True) as resp:
            if resp.status == 200:
                text = await resp.text()
                addr_match = re.search(r'"proxyWallet"\s*:\s*"(0x[a-fA-F0-9]{40})"', text)
                if addr_match:
                    return addr_match.group(1).lower()
                addr_match = re.search(r'"address"\s*:\s*"(0x[a-fA-F0-9]{40})"', text)
                if addr_match:
                    return addr_match.group(1).lower()
                final_url = str(resp.url)
                addr_match = re.search(r'/profile/(0x[a-fA-F0-9]{40})', final_url)
                if addr_match:
                    return addr_match.group(1).lower()
    except Exception as e:
        logger.error(f"Profile page error for {username}: {e}")

    return None


async def get_profile(session: aiohttp.ClientSession, address: str) -> dict | None:
    try:
        async with session.get(f"{GAMMA_API}/public-profile", params={"address": address}) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        logger.error(f"Profile error for {address}: {e}")
    return None


async def get_activity(session: aiohttp.ClientSession, address: str, limit: int = 50) -> list[dict]:
    params = {"user": address, "limit": str(limit), "sortBy": "TIMESTAMP", "sortDirection": "DESC"}
    try:
        async with session.get(f"{DATA_API}/activity", params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                if isinstance(data, list):
                    return data
                return data.get("history", data.get("data", []))
    except Exception as e:
        logger.error(f"Activity error for {address}: {e}")
    return []


async def detect_order_type(session: aiohttp.ClientSession, tx_hash: str, trader_address: str) -> str:
    """
    Determine if a trade was Limit or Market.

    OrderFilled event (Polymarket CTF Exchange):
        topic[0] = event signature hash
        topic[1] = orderHash (indexed bytes32)
        topic[2] = maker (indexed address)  ← Limit order placer
        topic[3] = taker (indexed address)  ← Market order executor
        data     = makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee

    If trader is maker → 📋 Limit
    If trader is taker → 📊 Market
    """
    tx_hash_hex = tx_hash if tx_hash.startswith("0x") else f"0x{tx_hash}"
    trader_clean = trader_address.lower().replace("0x", "").zfill(40)

    try:
        rpc_url = "https://polygon-bor-rpc.publicnode.com"
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash_hex],
            "id": 1,
        }
        async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return "❓"
            data = await resp.json()
            result = data.get("result")
            if not result:
                return "❓"

            logs = result.get("logs", [])

            is_maker = False
            is_taker = False

            for log in logs:
                topics = log.get("topics", [])
                if len(topics) < 4:
                    continue

                # topic[2] = maker (last 40 hex chars = address)
                # topic[3] = taker (last 40 hex chars = address)
                maker_addr = topics[2][-40:].lower()
                taker_addr = topics[3][-40:].lower()

                if maker_addr == trader_clean:
                    is_maker = True
                if taker_addr == trader_clean:
                    is_taker = True

            if is_maker and not is_taker:
                return "📋 Limit"
            elif is_taker and not is_maker:
                return "📊 Market"
            elif is_maker and is_taker:
                return "📋 Limit"

            return "❓"

    except Exception as e:
        logger.debug(f"Order type detection error: {e}")
        return "❓"
