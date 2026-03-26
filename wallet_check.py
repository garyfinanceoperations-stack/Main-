"""Quick diagnostic to figure out wallet/proxy setup for Polymarket."""

import os
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from eth_account import Account

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("CHAIN_ID", "137"))
FUNDER = os.getenv("FUNDER", "")

print("=" * 60)
print("  POLYMARKET WALLET DIAGNOSTIC")
print("=" * 60)

# Step 1: Show the EOA address
try:
    acct = Account.from_key(PRIVATE_KEY)
    print(f"\n1. Your EOA (private key) address: {acct.address}")
    print(f"   This is the address derived from your PRIVATE_KEY.")
except Exception as e:
    print(f"\n1. ERROR reading private key: {e}")
    print("   Make sure PRIVATE_KEY is set correctly in .env")
    exit(1)

if FUNDER:
    print(f"\n2. FUNDER (proxy wallet) set to: {FUNDER}")
else:
    print(f"\n2. FUNDER not set in .env")

# Step 2: Try each signature type
print("\n3. Testing each signature type to find your balance...\n")

for sig_type, sig_name in [(0, "EOA (direct wallet)"), (1, "Poly Proxy (email login)"), (2, "Gnosis Safe (MetaMask browser)")]:
    for funder_addr in ([FUNDER] if FUNDER else [""]):
        try:
            kwargs = {"host": HOST, "key": PRIVATE_KEY, "chain_id": CHAIN_ID, "signature_type": sig_type}
            if funder_addr:
                kwargs["funder"] = funder_addr

            client = ClobClient(**kwargs)
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = client.get_balance_allowance(params)

            balance = result.get("balance", "?") if isinstance(result, dict) else "?"
            label = f"sig_type={sig_type} ({sig_name})"
            if funder_addr:
                label += f" funder={funder_addr[:10]}..."

            marker = " <-- HAS FUNDS!" if balance not in ("0", "?", 0) else ""
            print(f"   {label}")
            print(f"   -> Balance: {balance}{marker}")

            if isinstance(result, dict) and result.get("allowances"):
                for addr, val in result["allowances"].items():
                    print(f"      Allowance {addr[:10]}...: {val}")
            print()

        except Exception as e:
            err = str(e)
            if len(err) > 100:
                err = err[:100] + "..."
            print(f"   sig_type={sig_type} ({sig_name}): ERROR - {err}\n")

# Step 3: Check if there's a Polymarket proxy address we can find
print("4. Checking for proxy wallet address via API...")
try:
    # Try sig_type=0 first to get basic info
    client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    # Check what methods might reveal the proxy address
    for method_name in ["get_address", "address", "get_proxy_wallet", "get_funder"]:
        if hasattr(client, method_name):
            try:
                result = getattr(client, method_name)
                if callable(result):
                    result = result()
                print(f"   {method_name}: {result}")
            except:
                pass

    # Check if there's a signer with an address
    if hasattr(client, "signer") and client.signer:
        if hasattr(client.signer, "address"):
            print(f"   Signer address: {client.signer.address()}")

    # Check builder
    if hasattr(client, "builder") and client.builder:
        if hasattr(client.builder, "funder"):
            print(f"   Builder funder: {client.builder.funder}")
        if hasattr(client.builder, "sig_type"):
            print(f"   Builder sig_type: {client.builder.sig_type}")

except Exception as e:
    print(f"   Error: {e}")

print("\n" + "=" * 60)
print("  WHAT TO DO NEXT:")
print("=" * 60)
print("""
If ALL balances show 0:
  -> Your Polymarket funds are in a proxy wallet.
  -> Go to polymarket.com, click your profile/wallet icon.
  -> Find your "deposit address" or "funding address".
  -> Add to .env: FUNDER=0xTHAT_ADDRESS_HERE
  -> Also add: SIGNATURE_TYPE=2 (for MetaMask) or 1 (for email)
  -> Then run this script again.

If one signature type shows a balance:
  -> Use that SIGNATURE_TYPE in your .env
  -> The bot will use it automatically.
""")
