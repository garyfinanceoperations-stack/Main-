"""Find your Polymarket proxy (Gnosis Safe) wallet address and test order signing."""

import os
import requests
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from eth_account import Account

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))

acct = Account.from_key(PRIVATE_KEY)
EOA = acct.address

print("=" * 60)
print("  FINDING YOUR POLYMARKET PROXY WALLET")
print("=" * 60)
print(f"\nYour EOA address: {EOA}")

# Method 1: Query Gnosis Safe Transaction Service for Safes owned by this EOA
print("\n--- Checking Gnosis Safe service for your proxy ---")
proxy_address = None

try:
    url = f"https://safe-transaction-polygon.safe.global/api/v1/owners/{EOA}/safes/"
    resp = requests.get(url, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        safes = data.get("safes", [])
        if safes:
            print(f"Found {len(safes)} Gnosis Safe(s) owned by your wallet:")
            for safe in safes:
                print(f"  -> {safe}")

            # Check which one has balance on Polymarket
            for safe_addr in safes:
                try:
                    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                                       signature_type=2, funder=safe_addr)
                    creds = client.create_or_derive_api_creds()
                    client.set_api_creds(creds)
                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    ba = client.get_balance_allowance(params)
                    balance = int(ba.get("balance", "0")) if isinstance(ba, dict) else 0
                    balance_usd = balance / 1_000_000
                    print(f"     Balance at {safe_addr}: ${balance_usd:.2f}")
                    if balance > 0:
                        proxy_address = safe_addr
                        print(f"     ^^^ THIS IS YOUR PROXY WALLET ^^^")
                except Exception as e:
                    print(f"     Error checking {safe_addr}: {e}")
        else:
            print("No Gnosis Safes found for your wallet.")
    else:
        print(f"Safe service returned {resp.status_code}")
except Exception as e:
    print(f"Error querying Safe service: {e}")

if not proxy_address:
    print("\nCould not find proxy automatically.")
    print("Go to polymarket.com -> Profile -> Settings/Wallet to find your deposit address.")
    exit(1)

# Test order signing with the proxy address
print(f"\n--- Testing order with FUNDER={proxy_address} ---")
try:
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
                        signature_type=2, funder=proxy_address)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    # Get a test market
    resp = requests.get(f"https://gamma-api.polymarket.com/markets",
                       params={"active": "true", "closed": "false", "limit": 10}, timeout=10)
    markets = resp.json()

    import json
    test_token = None
    for m in markets:
        tokens = m.get("clobTokenIds")
        if not tokens:
            continue
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except:
                continue
        if len(tokens) >= 2:
            test_token = tokens[0]
            test_name = m.get("question", "?")
            break

    if test_token:
        print(f"Test market: {test_name[:50]}")
        print(f"Placing $1 test order...")

        order_args = OrderArgs(
            price=0.10,
            size=10.0,
            side=BUY,
            token_id=test_token,
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        print(f"ORDER RESPONSE: {resp}")

        # Cancel immediately
        if isinstance(resp, dict) and resp.get("orderID"):
            oid = resp["orderID"]
            print(f"SUCCESS! Order placed: {oid}")
            client.cancel(oid)
            print("Test order cancelled.")
        elif isinstance(resp, dict) and resp.get("success") is False:
            print(f"Order rejected: {resp}")
        else:
            print(f"Response: {resp}")
    else:
        print("Could not find test market")

except Exception as e:
    print(f"Order test error: {e}")

print(f"\n{'=' * 60}")
print(f"  ADD THIS TO YOUR .env FILE:")
print(f"{'=' * 60}")
print(f"\n  FUNDER={proxy_address}")
print(f"  SIGNATURE_TYPE=2")
print(f"\n  Then run: python bot.py --test-order")
