"""Find your Polymarket proxy wallet address."""

import os
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
HOST = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")

print("Testing signature_type=2 with builder details...\n")

client = ClobClient(HOST, key=PRIVATE_KEY, chain_id=137, signature_type=2)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

print(f"API creds type: {type(creds)}")
print(f"API creds: {creds}")

if hasattr(client, "builder") and client.builder:
    b = client.builder
    print(f"\nBuilder funder: {b.funder}")
    print(f"Builder sig_type: {b.sig_type}")
    if hasattr(b, "signer"):
        print(f"Builder signer: {b.signer}")
    if hasattr(b, "maker_address"):
        print(f"Builder maker_address: {b.maker_address}")
    # Print all non-private attributes
    for attr in dir(b):
        if not attr.startswith("_"):
            try:
                val = getattr(b, attr)
                if not callable(val):
                    print(f"Builder.{attr}: {val}")
            except:
                pass

if hasattr(client, "signer") and client.signer:
    s = client.signer
    for attr in dir(s):
        if not attr.startswith("_"):
            try:
                val = getattr(s, attr)
                if not callable(val):
                    print(f"Signer.{attr}: {val}")
            except:
                pass

# Also check the creds object
if hasattr(creds, "__dict__"):
    print(f"\nCreds fields: {creds.__dict__}")
elif isinstance(creds, dict):
    print(f"\nCreds: {creds}")

# Try to get proxy address from CLOB API
print("\n--- Checking CLOB API for proxy address ---")
import requests
try:
    # Some CLOB APIs expose the proxy wallet mapping
    headers = {}
    if hasattr(client, "creds") and client.creds:
        c = client.creds
        if hasattr(c, "api_key"):
            headers["POLY_ADDRESS"] = client.builder.funder if hasattr(client, "builder") else ""
            headers["POLY_SIGNATURE"] = ""
            headers["POLY_TIMESTAMP"] = ""
            headers["POLY_NONCE"] = ""
            headers["POLY_API_KEY"] = c.api_key

    resp = requests.get(f"{HOST}/profile", timeout=5)
    print(f"  /profile: {resp.status_code} - {resp.text[:200] if resp.text else 'empty'}")
except Exception as e:
    print(f"  /profile: {e}")

print("\n--- Done ---")
print("\nThe 'Builder funder' address above is what you need for FUNDER in .env")
