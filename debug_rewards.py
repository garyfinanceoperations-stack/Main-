"""Debug script: dump the exact rewards data structure from CLOB API."""
import requests
import json

# Use a known condition_id from the simulation
# We'll fetch a few markets and show their full rewards structure
print("Fetching from Gamma API...")
resp = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"active": "true", "closed": "false", "limit": 10},
    timeout=15,
)
gamma_markets = resp.json()

found = 0
for m in gamma_markets:
    cid = m.get("conditionId", "")
    if not cid:
        continue

    # Show Gamma reward fields
    clob_rewards = m.get("clobRewards", [])
    if not clob_rewards:
        continue

    print(f"\n{'='*60}")
    print(f"Question: {m.get('question', '?')[:60]}")
    print(f"Condition: {cid}")
    print(f"\nGamma clobRewards:")
    print(json.dumps(clob_rewards, indent=2))

    # Also show other gamma reward fields
    for key in ["rewardsDailyRate", "rewardsDaily", "rewardsMinSize", "rewardsMaxSpread"]:
        val = m.get(key)
        if val is not None:
            print(f"Gamma {key}: {val}")

    # Now fetch from CLOB
    try:
        clob_resp = requests.get(
            f"https://clob.polymarket.com/markets/{cid}",
            timeout=10,
        )
        if clob_resp.status_code == 200:
            clob_data = clob_resp.json()
            rewards = clob_data.get("rewards", {})
            print(f"\nCLOB rewards object:")
            print(json.dumps(rewards, indent=2))

            # Show all keys in the rewards object
            if isinstance(rewards, dict):
                print(f"\nCLOB rewards keys: {list(rewards.keys())}")
                rates = rewards.get("rates")
                if rates:
                    print(f"rates type: {type(rates).__name__}")
                    print(f"rates value: {json.dumps(rates, indent=2)}")
        else:
            print(f"CLOB returned {clob_resp.status_code}")
    except Exception as e:
        print(f"CLOB error: {e}")

    found += 1
    if found >= 3:
        break

if found == 0:
    print("No markets with clobRewards found in first 10 gamma markets.")
    print("Trying CLOB directly...")
    for m in gamma_markets[:5]:
        cid = m.get("conditionId", "")
        if not cid:
            continue
        try:
            clob_resp = requests.get(
                f"https://clob.polymarket.com/markets/{cid}",
                timeout=10,
            )
            if clob_resp.status_code == 200:
                clob_data = clob_resp.json()
                rewards = clob_data.get("rewards", {})
                if rewards:
                    print(f"\nQuestion: {m.get('question', '?')[:60]}")
                    print(f"CLOB rewards: {json.dumps(rewards, indent=2)}")
                    break
        except:
            pass
