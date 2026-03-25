"""Diagnostic script - shows exactly what the API returns and where markets get filtered out."""

import requests
import json

session = requests.Session()
session.headers.update({"Accept": "application/json"})

print("=" * 60)
print("  POLYMARKET API DIAGNOSTIC")
print("=" * 60)

# 1. Test CLOB /markets
print("\n--- CLOB /markets ---")
try:
    resp = session.get("https://clob.polymarket.com/markets", timeout=30)
    print(f"Status: {resp.status_code}")
    data = resp.json()
    if isinstance(data, dict):
        print(f"Response type: dict with keys: {list(data.keys())[:10]}")
        # Try to find the actual list
        for key in ["data", "markets", "results"]:
            if key in data:
                data = data[key]
                print(f"Extracted list from key '{key}'")
                break
        if isinstance(data, dict):
            # Maybe it's paginated differently
            print(f"Still a dict. First 500 chars: {json.dumps(data)[:500]}")
    if isinstance(data, list):
        print(f"Got {len(data)} markets")
        if data:
            first = data[0]
            print(f"First market keys: {list(first.keys())}")
            print(f"Question: {first.get('question', first.get('title', 'N/A'))}")
            # Find reward-related keys
            reward_keys = [k for k in first.keys() if any(w in k.lower() for w in ['reward', 'incentive', 'liquidity'])]
            print(f"Reward-related keys: {reward_keys}")
            for k in reward_keys:
                print(f"  {k} = {first[k]}")
            # Token keys
            token_keys = [k for k in first.keys() if any(w in k.lower() for w in ['token', 'clob'])]
            print(f"Token-related keys: {token_keys}")
            for k in token_keys:
                val = first[k]
                if isinstance(val, list) and len(val) > 2:
                    val = val[:2]
                print(f"  {k} = {val}")
except Exception as e:
    print(f"Error: {e}")

# 2. Test Gamma /markets
print("\n--- Gamma /markets ---")
try:
    resp = session.get("https://gamma-api.polymarket.com/markets",
                       params={"active": True, "closed": False, "limit": 10}, timeout=30)
    print(f"Status: {resp.status_code}")
    data = resp.json()
    if isinstance(data, list):
        print(f"Got {len(data)} markets")
        if data:
            first = data[0]
            print(f"First market keys: {sorted(first.keys())}")
            print(f"Question: {first.get('question', first.get('title', 'N/A'))}")
            reward_keys = [k for k in first.keys() if any(w in k.lower() for w in ['reward', 'incentive', 'liquidity'])]
            print(f"Reward-related keys: {reward_keys}")
            for k in reward_keys:
                print(f"  {k} = {first[k]}")
            token_keys = [k for k in first.keys() if any(w in k.lower() for w in ['token', 'clob'])]
            print(f"Token-related keys: {token_keys}")
            for k in token_keys:
                val = first[k]
                if isinstance(val, (list, str)) and len(val) > 100:
                    val = str(val)[:100]
                print(f"  {k} = {val}")
            # Check conditionId
            print(f"conditionId: {first.get('conditionId', first.get('condition_id', 'MISSING'))}")
            # Show rewards object if nested
            if 'rewards' in first:
                print(f"rewards object: {json.dumps(first['rewards'])[:300]}")
    elif isinstance(data, dict):
        print(f"Response is dict with keys: {list(data.keys())}")
except Exception as e:
    print(f"Error: {e}")

# 3. Test CLOB /rewards endpoints
print("\n--- CLOB reward endpoints ---")
for path in ["/rewards/markets", "/rewards", "/incentives"]:
    try:
        resp = session.get(f"https://clob.polymarket.com{path}", timeout=15)
        print(f"  {path}: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"  {path}: {e}")

# 4. Gamma events
print("\n--- Gamma /events ---")
try:
    resp = session.get("https://gamma-api.polymarket.com/events",
                       params={"active": True, "closed": False, "limit": 3}, timeout=30)
    print(f"Status: {resp.status_code}")
    data = resp.json()
    if isinstance(data, list) and data:
        print(f"Got {len(data)} events")
        first = data[0]
        print(f"Event keys: {list(first.keys())}")
        markets = first.get("markets", [])
        print(f"Markets in first event: {len(markets)}")
        if markets:
            m = markets[0]
            print(f"Market keys: {sorted(m.keys())}")
            reward_keys = [k for k in m.keys() if any(w in k.lower() for w in ['reward', 'incentive', 'liquidity'])]
            print(f"Reward keys: {reward_keys}")
            for k in reward_keys:
                print(f"  {k} = {m[k]}")
            token_keys = [k for k in m.keys() if any(w in k.lower() for w in ['token', 'clob'])]
            print(f"Token keys: {token_keys}")
            for k in token_keys:
                print(f"  {k} = {m[k]}")
except Exception as e:
    print(f"Error: {e}")

# 5. Quick order book test with first available token
print("\n--- Order book test ---")
try:
    resp = session.get("https://gamma-api.polymarket.com/markets",
                       params={"active": True, "closed": False, "limit": 5}, timeout=30)
    data = resp.json()
    for m in data:
        tokens = m.get("clobTokenIds", m.get("clob_token_ids", []))
        if tokens and len(tokens) >= 2:
            token = tokens[0]
            print(f"Testing book for: {m.get('question', 'N/A')[:60]}")
            print(f"Token ID: {token[:30]}...")
            book = session.get(f"https://clob.polymarket.com/book",
                              params={"token_id": token}, timeout=15)
            print(f"Book status: {book.status_code}")
            bdata = book.json()
            bids = bdata.get("bids", [])
            asks = bdata.get("asks", [])
            print(f"Bids: {len(bids)}, Asks: {len(asks)}")
            if bids:
                print(f"Best bid: {bids[0]}")
            if asks:
                print(f"Best ask: {asks[0]}")
            break
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 60)
print("  DONE - share this output so we can fix the filters")
print("=" * 60)
