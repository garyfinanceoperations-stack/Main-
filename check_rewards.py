"""Check what reward fields the Polymarket API actually returns."""
import requests, json

print("=== Checking CLOB API reward fields ===\n")

# Check CLOB /markets for reward data
resp = requests.get("https://clob.polymarket.com/markets", timeout=15)
clob_markets = resp.json()
if isinstance(clob_markets, dict):
    clob_markets = clob_markets.get("data", clob_markets.get("markets", []))

# Find markets with any reward-related field
reward_markets = []
for m in clob_markets[:20]:
    reward_fields = {}
    for k, v in m.items():
        if any(rw in k.lower() for rw in ["reward", "incentive", "rate", "liquidity"]):
            reward_fields[k] = v
    if reward_fields:
        q = m.get("question", m.get("description", "?"))[:50]
        print(f"Market: {q}")
        print(f"  Reward fields: {json.dumps(reward_fields, indent=4)}")
        print()
        reward_markets.append(m)

if not reward_markets:
    print("No reward fields found in first 20 CLOB markets.")
    print("\nDumping ALL fields of first market:")
    if clob_markets:
        for k, v in clob_markets[0].items():
            val_str = str(v)[:100]
            print(f"  {k}: {val_str}")

print(f"\n=== Checking Gamma API ===\n")

resp = requests.get("https://gamma-api.polymarket.com/markets",
                    params={"active": "true", "closed": "false", "limit": 10}, timeout=15)
gamma_markets = resp.json()

for m in gamma_markets[:5]:
    reward_fields = {}
    for k, v in m.items():
        if any(rw in k.lower() for rw in ["reward", "incentive", "rate"]):
            reward_fields[k] = v
    q = m.get("question", "?")[:50]
    print(f"Market: {q}")
    if reward_fields:
        print(f"  Reward fields: {json.dumps(reward_fields, indent=4)}")
    else:
        print(f"  No reward fields found")
    print()

# Also check if there's a rewards-specific endpoint
print("=== Checking rewards endpoints ===\n")
for endpoint in ["/rewards/markets", "/rewards", "/incentives"]:
    try:
        r = requests.get(f"https://clob.polymarket.com{endpoint}", timeout=10)
        print(f"CLOB {endpoint}: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"CLOB {endpoint}: {e}")

for endpoint in ["/rewards/markets", "/rewards"]:
    try:
        r = requests.get(f"https://gamma-api.polymarket.com{endpoint}", timeout=10)
        print(f"Gamma {endpoint}: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        print(f"Gamma {endpoint}: {e}")
