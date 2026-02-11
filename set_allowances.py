"""
Run this ONCE before trading to set token allowances.
This lets Polymarket contracts interact with your USDC and conditional tokens.

Usage:
    pip install web3
    export PRIVATE_KEY=0x...
    python set_allowances.py
"""
import os
from web3 import Web3

# Polygon RPC (use multiple fallbacks)
RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
]

w3 = None
for rpc in RPCS:
    try:
        _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
        if _w3.is_connected():
            w3 = _w3
            print(f"✅ Connected to {rpc}")
            break
    except Exception:
        continue

if not w3:
    print("❌ Can't connect to any Polygon RPC")
    exit(1)

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
if not PRIVATE_KEY:
    print("❌ Set PRIVATE_KEY env var first!")
    exit(1)

account = w3.eth.account.from_key(PRIVATE_KEY)
print(f"🔑 Wallet: {account.address}")

# Contract addresses on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # CTF
CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # CLOB Exchange
NEG_RISK_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Neg Risk Exchange
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"  # Neg Risk Adapter

MAX_ALLOWANCE = 2**256 - 1

# ERC20 approve ABI
ERC20_ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]

# ERC1155 setApprovalForAll ABI
ERC1155_ABI = [{"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"}]


def approve_erc20(token_addr, spender, label=""):
    import time
    contract = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.5)  # 1.5x current gas for fast confirm
    print(f"   Gas price: {gas_price / 1e9:.1f} gwei")
    tx = contract.functions.approve(
        Web3.to_checksum_address(spender), MAX_ALLOWANCE
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"   Tx sent: {tx_hash.hex()}, waiting...")
    for _ in range(60):
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt:
                print(f"✅ {label}: confirmed!")
                return
        except Exception:
            pass
        time.sleep(3)
    print(f"⚠️ {label}: tx sent but timed out. Check polygonscan.")


def approve_erc1155(token_addr, operator, label=""):
    import time
    contract = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC1155_ABI)
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.5)
    print(f"   Gas price: {gas_price / 1e9:.1f} gwei")
    tx = contract.functions.setApprovalForAll(
        Web3.to_checksum_address(operator), True
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": gas_price,
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"   Tx sent: {tx_hash.hex()}, waiting...")
    for _ in range(60):
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt:
                print(f"✅ {label}: confirmed!")
                return
        except Exception:
            pass
        time.sleep(3)
    print(f"⚠️ {label}: tx sent but timed out. Check polygonscan.")


if __name__ == "__main__":
    import time

    print("Setting USDC allowances...")
    approve_erc20(USDC_ADDRESS, CTF_EXCHANGE, "USDC → Exchange")
    time.sleep(3)
    approve_erc20(USDC_ADDRESS, NEG_RISK_CTF_EXCHANGE, "USDC → NegRisk Exchange")
    time.sleep(3)

    print("\nSetting CTF (conditional token) allowances...")
    approve_erc1155(CTF_ADDRESS, CTF_EXCHANGE, "CTF → Exchange")
    time.sleep(3)
    approve_erc1155(CTF_ADDRESS, NEG_RISK_CTF_EXCHANGE, "CTF → NegRisk Exchange")
    time.sleep(3)
    approve_erc1155(CTF_ADDRESS, NEG_RISK_ADAPTER, "CTF → NegRisk Adapter")

    print("\n🎉 All allowances set! You can now trade.")