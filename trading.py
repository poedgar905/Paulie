"""
Trading module v2 — FOK buy, smart sell with retry, balance checks.
"""
import logging
import math
import time

from config import CLOB_API, CHAIN_ID, PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE

logger = logging.getLogger("trading")

_client = None
_client_ready = False


def _get_client():
    global _client, _client_ready
    if _client_ready:
        return _client
    if not PRIVATE_KEY:
        logger.warning("PRIVATE_KEY not set — trading disabled")
        _client_ready = True
        return None
    try:
        from py_clob_client.client import ClobClient
        _client = ClobClient(
            CLOB_API, key=PRIVATE_KEY, chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE, funder=FUNDER_ADDRESS,
        )
        _client.set_api_creds(_client.create_or_derive_api_creds())
        logger.info("CLOB client ready (funder=%s, sig=%s)", FUNDER_ADDRESS, SIGNATURE_TYPE)
    except Exception as e:
        logger.error("CLOB client init failed: %s", e)
        _client = None
    _client_ready = True
    return _client


def is_trading_enabled() -> bool:
    return _get_client() is not None


# ── Balance ──────────────────────────────────────────────────────

def get_balance() -> float | None:
    """Get USDC.e balance from Polygon."""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com",
                                     request_kwargs={"timeout": 10}))
        USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
                "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        contract = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)
        raw = contract.functions.balanceOf(Web3.to_checksum_address(FUNDER_ADDRESS)).call()
        return raw / 1e6
    except Exception as e:
        logger.error("Balance error: %s", e)
    return None


def get_conditional_balance(token_id: str) -> float | None:
    """Get real shares balance on-chain for a token."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = _get_client()
        if not client:
            return None
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        resp = client.get_balance_allowance(params)
        if resp and "balance" in resp:
            return float(resp["balance"]) / 1e6
    except Exception as e:
        logger.error("Conditional balance error: %s", e)
    return None


def debug_balance_info(token_id: str) -> str:
    """Debug info about balance and allowances."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = _get_client()
        if not client:
            return "Client not initialized"
        usdc = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        cond = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id))
        return f"USDC: {usdc}\nCond: {cond}"
    except Exception as e:
        return f"Debug error: {e}"


# ── Neg Risk Detection ───────────────────────────────────────────

_neg_risk_cache: dict[str, bool] = {}

def get_neg_risk(condition_id: str) -> bool:
    if condition_id in _neg_risk_cache:
        return _neg_risk_cache[condition_id]
    try:
        import requests
        resp = requests.get(f"https://gamma-api.polymarket.com/markets",
                          params={"condition_id": condition_id}, timeout=10)
        if resp.status_code == 200:
            markets = resp.json()
            if isinstance(markets, list) and markets:
                nr = markets[0].get("neg_risk", False)
                if isinstance(nr, str):
                    nr = nr.lower() == "true"
                _neg_risk_cache[condition_id] = bool(nr)
                return bool(nr)
    except Exception as e:
        logger.error("neg_risk check: %s", e)
    _neg_risk_cache[condition_id] = False
    return False


def get_token_id_for_market(condition_id: str, outcome: str) -> str | None:
    try:
        import requests, json
        resp = requests.get("https://gamma-api.polymarket.com/markets",
                          params={"condition_id": condition_id}, timeout=10)
        if resp.status_code == 200:
            markets = resp.json()
            if isinstance(markets, list) and markets:
                tokens = markets[0].get("clobTokenIds", "")
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except (json.JSONDecodeError, TypeError):
                        tokens = [t.strip() for t in tokens.split(",") if t.strip()]
                if isinstance(tokens, list) and len(tokens) >= 2:
                    return tokens[0] if outcome.lower() == "yes" else tokens[1]
                elif isinstance(tokens, list) and len(tokens) == 1:
                    return tokens[0]
    except Exception as e:
        logger.error("Token resolve error: %s", e)
    return None


# ── BUY — FOK (Fill-or-Kill) ────────────────────────────────────

def place_fok_buy(token_id: str, trader_price: float, amount_usdc: float,
                  condition_id: str = "", slippage: float = 0.015) -> dict | None:
    """
    FOK buy at trader_price + slippage.
    Either fills instantly or cancels — no capital lock.
    """
    client = _get_client()
    if not client:
        return None

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        buy_price = round(trader_price + slippage, 2)
        if buy_price >= 1.0:
            buy_price = 0.99
        if buy_price <= 0:
            return None

        size = math.ceil(amount_usdc * 1.02 / buy_price * 100) / 100
        if size < 5:
            size = 5.0

        neg_risk = get_neg_risk(condition_id) if condition_id else False

        bal = get_balance()
        actual_cost = round(size * buy_price, 2)
        logger.info("FOK BUY: %.2f¢ (+%.1f¢ slip), %s sh, $%.2f, bal=$%s, neg_risk=%s",
                     buy_price * 100, slippage * 100, size, actual_cost,
                     f"{bal:.2f}" if bal else "?", neg_risk)

        order_args = OrderArgs(price=buy_price, size=size, side=BUY, token_id=token_id)
        signed = client.create_order(order_args)

        # Try FOK first, fallback to GTC if FOK not supported
        resp = None
        try:
            resp = client.post_order(signed, orderType=OrderType.FOK)
        except (TypeError, AttributeError):
            try:
                resp = client.post_order(signed, "FOK")
            except Exception:
                logger.warning("FOK not available, falling back to GTC")
                try:
                    resp = client.post_order(signed, orderType=OrderType.GTC)
                except TypeError:
                    resp = client.post_order(signed, OrderType.GTC)

        logger.info("FOK BUY resp: %s", resp)

        if not resp:
            return None

        status = resp.get("status", "")
        order_id = resp.get("orderID", "")

        if status == "matched":
            return {
                "order_id": order_id, "price": buy_price, "size": size,
                "status": "FILLED", "response": resp,
            }
        elif status == "live":
            # GTC fallback — order is pending
            return {
                "order_id": order_id, "price": buy_price, "size": size,
                "status": "PENDING", "response": resp,
            }
        else:
            # FOK killed — no fill, no capital lock
            logger.info("FOK killed (no liquidity at %.2f¢)", buy_price * 100)
            return None

    except Exception as e:
        logger.error("FOK BUY error: %s", e)
        return None


# ── SELL — Smart with retry ──────────────────────────────────────

def smart_sell(token_id: str, shares: float, trader_sell_price: float,
               condition_id: str = "") -> dict | None:
    """
    Smart sell with 3 levels:
    1. Limit sell at trader_price - 2¢ (try to get close to his price)
    2. If no fill in 10s → lower by 5¢
    3. If still no fill → market sell (1¢)
    """
    # First check real balance
    real_bal = get_conditional_balance(token_id)
    if real_bal is not None and real_bal < 0.1:
        logger.warning("No shares on-chain (bal=%.2f), skipping sell", real_bal)
        return {"status": "ghost", "shares": 0}

    if real_bal is not None and real_bal < shares:
        logger.info("Adjusting sell: DB=%.1f, on-chain=%.1f", shares, real_bal)
        shares = round(real_bal, 2)

    if shares < 0.1:
        return {"status": "ghost", "shares": 0}

    neg_risk = get_neg_risk(condition_id) if condition_id else False

    # Level 1: limit at trader_price - 2¢
    if trader_sell_price > 0.05:
        price1 = round(trader_sell_price - 0.02, 2)
        result = _try_sell(token_id, shares, price1, neg_risk)
        if result and result.get("status") == "matched":
            logger.info("SELL L1 filled @ %.2f¢", price1 * 100)
            return result

        # Wait 8 seconds for fill
        order_id = result.get("order_id", "") if result else ""
        if order_id:
            time.sleep(8)
            status = check_order_status(order_id)
            if status and status.lower() == "matched":
                logger.info("SELL L1 filled after wait @ %.2f¢", price1 * 100)
                return {"order_id": order_id, "price": price1, "size": shares, "status": "matched"}
            # Cancel L1
            cancel_order(order_id)

    # Level 2: limit at trader_price - 7¢
    if trader_sell_price > 0.10:
        price2 = round(trader_sell_price - 0.07, 2)
        result = _try_sell(token_id, shares, price2, neg_risk)
        if result and result.get("status") == "matched":
            logger.info("SELL L2 filled @ %.2f¢", price2 * 100)
            return result

        order_id = result.get("order_id", "") if result else ""
        if order_id:
            time.sleep(5)
            status = check_order_status(order_id)
            if status and status.lower() == "matched":
                logger.info("SELL L2 filled after wait @ %.2f¢", price2 * 100)
                return {"order_id": order_id, "price": price2, "size": shares, "status": "matched"}
            cancel_order(order_id)

    # Level 3: market sell (1¢)
    logger.info("SELL L3: market sell @ 1¢")
    result = _try_sell(token_id, shares, 0.01, neg_risk)
    if result:
        logger.info("SELL L3 result: %s", result.get("status"))
    return result


def _try_sell(token_id: str, size: float, price: float, neg_risk: bool) -> dict | None:
    """Single sell attempt."""
    client = _get_client()
    if not client:
        return None
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        price = round(price, 2)
        size = round(size, 2)
        if size < 0.1 or price <= 0 or price >= 1:
            return None

        order_args = OrderArgs(price=price, size=size, side=SELL, token_id=token_id)
        signed = client.create_order(order_args)

        try:
            resp = client.post_order(signed, orderType=OrderType.GTC)
        except TypeError:
            resp = client.post_order(signed, OrderType.GTC)

        logger.info("SELL @ %.2f¢: %s", price * 100, resp)

        if resp and resp.get("orderID"):
            return {
                "order_id": resp.get("orderID", ""),
                "price": price, "size": size,
                "status": resp.get("status", ""),
                "response": resp,
            }
        if resp and resp.get("status") == "matched":
            return {
                "order_id": resp.get("orderID", ""),
                "price": price, "size": size,
                "status": "matched", "response": resp,
            }
        return None
    except Exception as e:
        logger.error("SELL error @ %.2f¢: %s", price * 100, e)
        return None


def place_market_sell(token_id: str, size: float, condition_id: str = "") -> dict | None:
    """Backward compat — market sell at 1¢."""
    return _try_sell(token_id, size, 0.01,
                     get_neg_risk(condition_id) if condition_id else False)


# ── Order Management ─────────────────────────────────────────────

def check_order_status(order_id: str) -> str | None:
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.get_order(order_id)
        return resp.get("status", None)
    except Exception as e:
        logger.error("Order status error %s: %s", order_id[:20], e)
        return None


def get_open_orders() -> list:
    client = _get_client()
    if not client:
        return []
    try:
        resp = client.get_orders()
        return resp if isinstance(resp, list) else []
    except Exception as e:
        logger.error("Open orders error: %s", e)
        return []


def cancel_order(order_id: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        resp = client.cancel(order_id)
        logger.info("Cancelled %s: %s", order_id[:20], resp)
        return True
    except Exception as e:
        logger.error("Cancel error %s: %s", order_id[:20], e)
        return False


# ── Auto Allowances ──────────────────────────────────────────────

def ensure_allowances():
    """Check and set USDC + CTF allowances if needed."""
    _auto_set_usdc_allowances()
    _auto_set_ctf_allowances()


def _auto_set_usdc_allowances():
    try:
        from web3 import Web3
        w3 = None
        for rpc in ["https://polygon-bor-rpc.publicnode.com", "https://polygon.llamarpc.com"]:
            try:
                _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                if _w3.is_connected():
                    w3 = _w3
                    break
            except Exception:
                continue
        if not w3:
            return

        account = w3.eth.account.from_key(PRIVATE_KEY)
        MAX = 2**256 - 1
        USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        SPENDERS = [
            ("0xC5d563A36AE78145C45a50134d48A1215220f80a", "Exchange"),
            ("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", "NegRisk"),
        ]
        ABI = [
            {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
             "name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
            {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
             "name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        ]
        usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=ABI)
        nonce = w3.eth.get_transaction_count(account.address)

        for addr, label in SPENDERS:
            current = usdc.functions.allowance(account.address, Web3.to_checksum_address(addr)).call()
            if current < 10**12:
                gas_price = int(w3.eth.gas_price * 1.5)
                tx = usdc.functions.approve(Web3.to_checksum_address(addr), MAX).build_transaction({
                    "from": account.address, "nonce": nonce, "gas": 100000, "gasPrice": gas_price,
                })
                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                w3.eth.send_raw_transaction(signed.raw_transaction)
                logger.info("USDC approve %s sent", label)
                nonce += 1
                time.sleep(5)
    except Exception as e:
        logger.error("USDC approve error: %s", e)


def _auto_set_ctf_allowances():
    try:
        from web3 import Web3
        w3 = None
        for rpc in ["https://polygon-bor-rpc.publicnode.com", "https://polygon.llamarpc.com"]:
            try:
                _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                if _w3.is_connected():
                    w3 = _w3
                    break
            except Exception:
                continue
        if not w3:
            return

        account = w3.eth.account.from_key(PRIVATE_KEY)
        CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        OPERATORS = [
            ("0xC5d563A36AE78145C45a50134d48A1215220f80a", "Exchange"),
            ("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", "NegRisk"),
            ("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", "Adapter"),
        ]
        ABI = [
            {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
             "name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"},
            {"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],
             "name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
        ]
        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=ABI)
        nonce = w3.eth.get_transaction_count(account.address)

        for addr, label in OPERATORS:
            ok = ctf.functions.isApprovedForAll(account.address, Web3.to_checksum_address(addr)).call()
            if not ok:
                gas_price = int(w3.eth.gas_price * 1.5)
                tx = ctf.functions.setApprovalForAll(Web3.to_checksum_address(addr), True).build_transaction({
                    "from": account.address, "nonce": nonce, "gas": 100000, "gasPrice": gas_price,
                })
                signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
                w3.eth.send_raw_transaction(signed.raw_transaction)
                logger.info("CTF approve %s sent", label)
                nonce += 1
                time.sleep(5)
    except Exception as e:
        logger.error("CTF approve error: %s", e)
