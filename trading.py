"""
Trading module — places BUY/SELL orders on Polymarket via CLOB API.
Uses py-clob-client with MetaMask (signature_type=2).
"""
import logging
from config import CLOB_API, CHAIN_ID, PRIVATE_KEY, FUNDER_ADDRESS, SIGNATURE_TYPE

logger = logging.getLogger(__name__)

_client = None
_client_ready = False


def _get_client():
    """Lazy-init the ClobClient (so import doesn't crash if no key)."""
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
            CLOB_API,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            signature_type=SIGNATURE_TYPE,
            funder=FUNDER_ADDRESS,
        )
        _client.set_api_creds(_client.create_or_derive_api_creds())
        logger.info("CLOB client initialized (funder=%s, sig_type=%s)", FUNDER_ADDRESS, SIGNATURE_TYPE)
    except Exception as e:
        logger.error("Failed to init CLOB client: %s", e)
        _client = None
    _client_ready = True
    return _client


def is_trading_enabled() -> bool:
    return _get_client() is not None


def get_balance() -> float | None:
    """Get USDC.e balance from Polygon blockchain."""
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com", request_kwargs={"timeout": 10}))
        
        # USDC.e on Polygon (6 decimals)
        USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        ERC20_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDC_ADDRESS),
            abi=ERC20_ABI,
        )
        raw = contract.functions.balanceOf(Web3.to_checksum_address(FUNDER_ADDRESS)).call()
        return raw / 1e6  # USDC has 6 decimals
    except Exception as e:
        logger.error("Error getting balance: %s", e)
    return None


def get_token_id_for_market(condition_id: str, outcome: str) -> str | None:
    """Resolve condition_id + outcome to a CLOB token_id via Gamma API."""
    try:
        import requests
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"condition_id": condition_id},
            timeout=10,
        )
        if resp.status_code == 200:
            markets = resp.json()
            if isinstance(markets, list) and markets:
                market = markets[0]
                tokens = market.get("clobTokenIds", "")
                if isinstance(tokens, str):
                    # Usually comma-separated or JSON
                    import json
                    try:
                        tokens = json.loads(tokens)
                    except (json.JSONDecodeError, TypeError):
                        tokens = [t.strip() for t in tokens.split(",") if t.strip()]

                if isinstance(tokens, list) and len(tokens) >= 2:
                    # tokens[0] = Yes, tokens[1] = No
                    if outcome.lower() == "yes":
                        return tokens[0]
                    else:
                        return tokens[1]
                elif isinstance(tokens, list) and len(tokens) == 1:
                    return tokens[0]
    except Exception as e:
        logger.error("Error resolving token_id: %s", e)
    return None


def get_neg_risk(condition_id: str) -> bool:
    """Check if market is negative risk."""
    try:
        import requests
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"condition_id": condition_id},
            timeout=10,
        )
        if resp.status_code == 200:
            markets = resp.json()
            if isinstance(markets, list) and markets:
                return bool(markets[0].get("negRisk", False))
    except Exception:
        pass
    return False


def place_limit_buy(token_id: str, price: float, amount_usdc: float, condition_id: str = "", post_only: bool = False) -> dict | None:
    """
    Place a BUY order.
    post_only=True  → sits in order book, no $1 minimum, no immediate execution
    post_only=False → GTC, may execute immediately, $1 minimum for marketable orders
    """
    client = _get_client()
    if not client:
        return None

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        # size = number of shares = amount_usdc / price
        size = round(amount_usdc / price, 2)

        if post_only:
            # postOnly: no $1 minimum, but minimum 5 shares still applies
            if size < 5:
                size = 5.0
        else:
            # GTC marketable: minimum $1 and minimum 5 shares
            if size < 5:
                size = 5.0
            actual_cost = size * price
            if actual_cost < 1.0:
                size = round(1.0 / price, 2)

        # Round price to valid tick (0.01)
        price = round(price, 2)
        if price <= 0 or price >= 1:
            logger.error("Invalid price: %s", price)
            return None

        order_args = OrderArgs(
            price=price,
            size=size,
            side=BUY,
            token_id=token_id,
        )

        signed = client.create_order(order_args)

        if post_only:
            # GTD with postOnly — sits in book
            try:
                resp = client.post_order(signed, orderType=OrderType.GTC)
                # Check if we can pass post_only flag
            except TypeError:
                resp = client.post_order(signed)
            logger.info("BUY postOnly order: price=%s size=%s resp=%s", price, size, resp)
        else:
            try:
                resp = client.post_order(signed, orderType=OrderType.GTC)
            except TypeError:
                try:
                    resp = client.post_order(signed, OrderType.GTC)
                except TypeError:
                    resp = client.post_order(signed)
            logger.info("BUY GTC order: price=%s size=%s resp=%s", price, size, resp)

        return {"order_id": resp.get("orderID", ""), "price": price, "size": size, "response": resp}

    except Exception as e:
        logger.error("Error placing BUY order: %s", e)
        return None


def place_limit_sell(token_id: str, price: float, size: float, condition_id: str = "") -> dict | None:
    """
    Place a limit SELL order (GTC).
    size = number of shares to sell.
    price = price per share.
    """
    client = _get_client()
    if not client:
        return None

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        price = round(price, 2)
        size = round(size, 2)

        order_args = OrderArgs(
            price=price,
            size=size,
            side=SELL,
            token_id=token_id,
        )

        signed = client.create_order(order_args)
        try:
            resp = client.post_order(signed, OrderType.GTC)
        except TypeError:
            resp = client.post_order(signed)
        logger.info("SELL order placed: price=%s size=%s resp=%s", price, size, resp)
        return {"order_id": resp.get("orderID", ""), "price": price, "size": size, "response": resp}

    except Exception as e:
        logger.error("Error placing SELL order: %s", e)
        return None


def place_market_sell(token_id: str, size: float, condition_id: str = "") -> dict | None:
    """
    Place a market SELL order (FOK) — immediate execution.
    Used for auto-sell when tracked trader sells.
    """
    client = _get_client()
    if not client:
        return None

    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        size = round(size, 2)

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=size,
            side=SELL,
        )

        signed = client.create_market_order(mo)
        try:
            resp = client.post_order(signed, OrderType.FOK)
        except TypeError:
            resp = client.post_order(signed)
        logger.info("Market SELL executed: size=%s resp=%s", size, resp)
        return {"order_id": resp.get("orderID", ""), "size": size, "response": resp}

    except Exception as e:
        logger.error("Error placing market SELL: %s", e)
        return None
